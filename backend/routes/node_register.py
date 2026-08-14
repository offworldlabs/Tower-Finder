"""POST /v1/nodes/register, the one-off handshake that mints a node's token.

The router carries no prefix: `routes/nodes.py` supplies it at mount. The
handler lands here and answers 200 on success.
"""

from fastapi import APIRouter

router = APIRouter()
