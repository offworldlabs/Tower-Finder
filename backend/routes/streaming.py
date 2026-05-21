"""WebSocket and SSE live-streaming endpoints."""

import asyncio
import hmac
import logging
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from core import state
from core.auth import get_user_nodes
from core.users import AUTH_BYPASS, read_user_from_token
from services.tasks.aircraft_flush import filter_payload_to_nodes

router = APIRouter()

_WS_AUTH_TOKEN = os.getenv("WS_AUTH_TOKEN", "")


async def _authenticate_ws(ws: WebSocket) -> bool:
    """Validate WebSocket token if WS_AUTH_TOKEN is configured.

    Returns True if the connection is authorized (or auth is disabled).
    Closes the socket and returns False on failure.
    """
    if not _WS_AUTH_TOKEN:
        return True
    token = ws.query_params.get("token", "")
    if not token or not hmac.compare_digest(token, _WS_AUTH_TOKEN):
        await ws.close(code=1008, reason="Unauthorized")
        return False
    return True


@router.websocket("/ws/aircraft")
async def websocket_aircraft(ws: WebSocket):
    """Full aircraft feed — all nodes including synthetic simulation fleet."""
    if not await _authenticate_ws(ws):
        return
    await ws.accept()
    state.ws_clients.add(ws)
    logging.info("WebSocket client connected (%d total)", len(state.ws_clients))
    try:
        if state.latest_aircraft_json.get("aircraft"):
            await ws.send_text(state.latest_aircraft_json_bytes.decode())
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        state.ws_clients.discard(ws)
        logging.info("WebSocket client disconnected (%d remaining)", len(state.ws_clients))


@router.websocket("/ws/aircraft/live")
async def websocket_aircraft_live(ws: WebSocket):
    """Real-node-only aircraft feed — excludes synthetic simulation nodes.
    Used by map.retina.fm showing only radar3.retnode.com data.
    """
    if not await _authenticate_ws(ws):
        return
    await ws.accept()
    state.ws_live_clients.add(ws)
    logging.info("WS live client connected (%d total)", len(state.ws_live_clients))
    try:
        # Send current snapshot immediately so the map doesn't start blank
        if state.latest_real_aircraft_json_bytes:
            await ws.send_text(state.latest_real_aircraft_json_bytes.decode())
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        state.ws_live_clients.discard(ws)
        logging.info("WS live client disconnected (%d remaining)", len(state.ws_live_clients))


@router.websocket("/ws/aircraft/owner")
async def websocket_aircraft_owner(ws: WebSocket):
    """Per-owner aircraft feed — only aircraft/arcs from the caller's own nodes.

    Authenticated via the same auth_token cookie as the dashboard (sent
    automatically on same-origin WS handshakes). Unlike the public feeds this
    is true server-side data isolation: the connection never receives data for
    nodes the user doesn't own.
    """
    if not await _authenticate_ws(ws):
        return
    if AUTH_BYPASS:
        # Dev/staging with no OAuth: the anonymous admin owns whatever rows
        # are assigned to the all-zero uuid (usually nothing).
        owned = set(await get_user_nodes("00000000-0000-0000-0000-000000000000"))
    else:
        user = await read_user_from_token(ws.cookies.get("auth_token"))
        if user is None or not user.is_active:
            await ws.close(code=1008, reason="Unauthorized")
            return
        owned = set(await get_user_nodes(str(user.id)))

    await ws.accept()
    state.ws_owner_clients[ws] = owned
    logging.info("WS owner client connected (%d nodes, %d total)", len(owned), len(state.ws_owner_clients))
    try:
        if state.latest_aircraft_json.get("aircraft"):
            await ws.send_text(filter_payload_to_nodes(state.latest_aircraft_json, owned).decode())
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        state.ws_owner_clients.pop(ws, None)
        logging.info("WS owner client disconnected (%d remaining)", len(state.ws_owner_clients))


@router.get("/api/radar/stream")
async def sse_aircraft_stream():
    async def _generate():
        last_hash = ""
        while True:
            data = state.latest_aircraft_json
            current_hash = str(data.get("now", 0))
            if current_hash != last_hash:
                yield b"data: " + state.latest_aircraft_json_bytes + b"\n\n"
                last_hash = current_hash
            await asyncio.sleep(1)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
