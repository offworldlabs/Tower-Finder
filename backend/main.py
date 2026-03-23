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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from pipeline.passive_radar import PassiveRadarPipeline, DEFAULT_NODE_CONFIG
from core import state
from services.tcp_handler import handle_tcp_client
from services.background import (
    frame_processor_loop,
    aircraft_flush_task,
    archive_flush_task,
    reputation_evaluator,
    adsb_truth_fetcher,
)
from routes.towers import router as towers_router
from routes.stats import router as stats_router
from routes.radar import router as radar_router
from routes.analytics import router as analytics_router
from routes.streaming import router as streaming_router
from routes.archive import router as archive_router
from routes.test import router as test_router
from routes.custody import router as custody_router
from routes.auth import router as auth_router
from routes.admin import router as admin_router

load_dotenv()
logging.basicConfig(level=logging.INFO)

TCP_PORT = int(os.getenv("RADAR_TCP_PORT", "3012"))

# ── Global pipeline (default geometry for file-loaded data) ───────────────────
_TAR1090_DATA_DIR = os.path.join(os.path.dirname(__file__), "tar1090_data")
os.makedirs(_TAR1090_DATA_DIR, exist_ok=True)

radar_pipeline = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)

# Write initial receiver.json
with open(os.path.join(_TAR1090_DATA_DIR, "receiver.json"), "w") as _f:
    json.dump(radar_pipeline.generate_receiver_json(), _f)

# Inject pipeline reference into route modules that need it
from routes import radar as _radar_mod, test as _test_mod  # noqa: E402
_radar_mod.init(radar_pipeline)
_test_mod.init(radar_pipeline)


# ── Lifespan: TCP server + background tasks ───────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    server = await asyncio.start_server(handle_tcp_client, "0.0.0.0", TCP_PORT)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    logging.info("Radar TCP server listening on %s", addrs)
    async with server:
        # Run multiple parallel frame processor workers so the thread pool can
        # process frames concurrently (scipy/numpy release the GIL).
        _n_frame_workers = int(os.environ.get("FRAME_WORKERS", "4"))
        tasks = [
            asyncio.create_task(server.serve_forever()),
            asyncio.create_task(reputation_evaluator()),
            asyncio.create_task(adsb_truth_fetcher()),
            asyncio.create_task(aircraft_flush_task(radar_pipeline)),
            asyncio.create_task(archive_flush_task()),
            *[asyncio.create_task(frame_processor_loop(radar_pipeline))
              for _ in range(_n_frame_workers)],
        ]
        yield
        for t in tasks:
            t.cancel()
        # Flush remaining buffered archives before exit
        from services.frame_processor import flush_all_archive_buffers
        flush_all_archive_buffers()
        state.node_analytics.save_coverage_maps()
        logging.info("Coverage maps saved to %s", state.COVERAGE_STORAGE_DIR)


# ── App factory ───────────────────────────────────────────────────────────────

app = FastAPI(title="Tower Finder API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://localhost:3000,http://localhost:5174,"
        "https://retina.fm,https://api.retina.fm,https://dash.retina.fm,"
        "https://admin.retina.fm,https://testapi.retina.fm,https://testmap.retina.fm",
    ).split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Mount all routers ─────────────────────────────────────────────────────────
for router in (
    towers_router, stats_router, radar_router, analytics_router,
    streaming_router, archive_router, test_router, custody_router,
    auth_router, admin_router,
):
    app.include_router(router)

