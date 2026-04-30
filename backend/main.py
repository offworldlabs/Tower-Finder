"""Tower Finder API — slim app factory.

All business logic lives in dedicated packages:
  core/       – shared mutable state
  services/   – TCP handler, frame processor, background tasks, storage
  clients/    – external API clients (FCC, Maprad, OpenSky)
  analytics/  – node trust, reputation, coverage, cross-node analysis
  pipeline/   – passive radar signal processing
  routes/     – FastAPI APIRouter modules
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from core import state
from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline
from routes.admin import router as admin_router
from routes.analytics import router as analytics_router
from routes.archive import router as archive_router
from routes.auth import router as auth_router
from routes.custody import router as custody_router
from routes.output import router as output_router
from routes.radar import router as radar_router
from routes.stats import router as stats_router
from routes.streaming import router as streaming_router
from routes.test import router as test_router
from routes.towers import router as towers_router
from services.background import (
    adsb_truth_fetcher,
    aircraft_flush_task,
    analytics_refresh_task,
    archive_flush_task,
    archive_lifecycle_task,
    frame_processor_loop,
    reputation_evaluator,
    start_solver_workers,
    storage_refresh_task,
)
from services.blah2_bridge import blah2_bridge_task
from services.runtime_coverage import start as _start_coverage
from services.runtime_coverage import stop as _stop_coverage
from services.state_snapshot import SAVE_INTERVAL_S, restore_snapshot, save_snapshot
from services.tcp_handler import handle_tcp_client

load_dotenv()
logging.basicConfig(level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))

TCP_PORT = int(os.getenv("RADAR_TCP_PORT", "3012"))

# ── Global pipeline (default geometry for file-loaded data) ───────────────────
_TAR1090_DATA_DIR = os.path.join(os.path.dirname(__file__), "tar1090_data")
os.makedirs(_TAR1090_DATA_DIR, exist_ok=True)

radar_pipeline = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)

# Write initial receiver.json
with open(os.path.join(_TAR1090_DATA_DIR, "receiver.json"), "w") as _f:
    json.dump(radar_pipeline.generate_receiver_json(), _f)

# Inject pipeline reference into route modules that need it
from routes import radar as _radar_mod  # noqa: E402
from routes import test as _test_mod

_radar_mod.init(radar_pipeline)
_test_mod.init(radar_pipeline)


# ── Lifespan: TCP server + background tasks ───────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start runtime coverage if COVERAGE_ENABLED=1
    _start_coverage()

    # Restore persisted state before accepting connections
    restored = restore_snapshot()

    from services.alerting import send_alert
    send_alert("server_start", "RETINA server started", {"restored": restored})

    server = await asyncio.start_server(handle_tcp_client, "0.0.0.0", TCP_PORT)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    logging.info("Radar TCP server listening on %s", addrs)
    async with server:
        # Start background daemon threads for multinode LM solving.
        # These drain solver_queue independently of frame workers.
        start_solver_workers()
        # Run multiple parallel frame processor workers so the thread pool can
        # process frames concurrently (scipy/numpy release the GIL).
        _n_frame_workers = int(os.environ.get("FRAME_WORKERS", "4"))

        async def _snapshot_loop():
            """Save state snapshot periodically."""
            while True:
                await asyncio.sleep(SAVE_INTERVAL_S)
                try:
                    await asyncio.get_event_loop().run_in_executor(None, save_snapshot)
                except Exception:
                    logging.exception("State snapshot save failed")

        tasks = [
            asyncio.create_task(server.serve_forever()),
            asyncio.create_task(reputation_evaluator()),
            asyncio.create_task(adsb_truth_fetcher()),
            asyncio.create_task(aircraft_flush_task(radar_pipeline)),
            asyncio.create_task(archive_flush_task()),
            asyncio.create_task(archive_lifecycle_task()),
            asyncio.create_task(analytics_refresh_task()),
            asyncio.create_task(storage_refresh_task()),
            asyncio.create_task(blah2_bridge_task()),
            asyncio.create_task(_snapshot_loop()),
            *[asyncio.create_task(frame_processor_loop(radar_pipeline))
              for _ in range(_n_frame_workers)],
        ]
        yield
        for t in tasks:
            t.cancel()
        # Save state snapshot before exit
        try:
            save_snapshot()
        except Exception:
            logging.exception("Final state snapshot failed")
        # Flush remaining buffered archives before exit
        from services.frame_processor import flush_all_archive_buffers
        flush_all_archive_buffers()
        state.node_analytics.save_coverage_maps()
        # Stop runtime coverage and flush report
        _stop_coverage()
        logging.info("Coverage maps saved to %s", state.COVERAGE_STORAGE_DIR)


# ── App factory ───────────────────────────────────────────────────────────────

_MAX_BODY_BYTES = int(os.getenv("MAX_REQUEST_BODY_BYTES", str(5 * 1024 * 1024)))  # 5 MB


class LimitUploadSize(BaseHTTPMiddleware):
    """Reject requests with Content-Length exceeding the configured limit."""

    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl and int(cl) > _MAX_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Request body too large (max {_MAX_BODY_BYTES} bytes)"},
            )
        return await call_next(request)


app = FastAPI(title="Tower Finder API", lifespan=lifespan)

app.add_middleware(LimitUploadSize)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://localhost:3000,http://localhost:5174,"
        "https://retina.fm,https://api.retina.fm,https://dash.retina.fm,"
        "https://admin.retina.fm,https://testapi.retina.fm,https://testmap.retina.fm,"
        "https://towers.retina.fm,https://map.retina.fm",
    ).split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key"],
    max_age=3600,
)

# ── Mount all routers ─────────────────────────────────────────────────────────
for router in (
    towers_router, stats_router, radar_router, analytics_router,
    streaming_router, archive_router, test_router, custody_router,
    auth_router, admin_router, output_router,
):
    app.include_router(router)

