"""PUT /v1/nodes/config, where a node resends its full configuration.

The router carries no prefix: `routes/nodes.py` supplies it at mount. The
handler lands here and answers 200 on success.

Distinct from `services/node_config.py`, which holds the configuration service
this route will call. Import that one by its full path to keep the two apart.
"""

from fastapi import APIRouter

router = APIRouter()
