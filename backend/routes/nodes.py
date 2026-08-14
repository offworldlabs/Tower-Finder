"""The v1 node API: prefix, tags and body caps for the four node endpoints.

The handlers live in three sibling modules mounted below, so that work on one
endpoint never touches another's lines. This module holds only the wiring.
"""

from fastapi import APIRouter

from routes.node_config import router as node_config_router
from routes.node_register import router as node_register_router
from routes.node_stream import router as node_stream_router

router = APIRouter(prefix="/v1/nodes", tags=["nodes"])

router.include_router(node_register_router)
router.include_router(node_config_router)
router.include_router(node_stream_router)

# Per-path request body caps, transcribed from `nodes_api_v1.yml` ("Request
# bodies are size-capped"): 8 KiB for registration, heartbeat and configuration,
# 64 KiB for a detection frame. The array and string bounds in the spec's
# schemas are the same limits expressed per field. Keys are full app paths,
# since the contract's server URL already carries the `/v1` prefix.
NODE_BODY_LIMITS: dict[str, int] = {
    "/v1/nodes/register": 8 * 1024,
    "/v1/nodes/config": 8 * 1024,
    "/v1/nodes/heartbeat": 8 * 1024,
    "/v1/nodes/detection": 64 * 1024,
}
