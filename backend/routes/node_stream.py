"""The two streaming-tag paths: POST /v1/nodes/detection and /v1/nodes/heartbeat.

The router carries no prefix: `routes/nodes.py` supplies it at mount. The
handlers land here; detection answers 202, heartbeat 200.
"""

from fastapi import APIRouter

router = APIRouter()
