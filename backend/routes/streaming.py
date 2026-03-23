"""WebSocket and SSE live-streaming endpoints."""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from core import state

router = APIRouter()


@router.websocket("/ws/aircraft")
async def websocket_aircraft(ws: WebSocket):
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
            await asyncio.sleep(2)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
