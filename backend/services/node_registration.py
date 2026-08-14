"""One place to register a node without stalling the event loop.

`InterNodeAssociator.register_node` pre-computes an overlap grid against every
node already registered. On the production fleet geometry that is seconds of
CPU for a single node, so it cannot run on the event loop: it would hold up
every other request in flight, not just the one that triggered it.

Single-threaded on purpose. Registration is serialised by the associator's own
lock regardless, so extra threads buy nothing, and one dedicated thread keeps
registration from starving the default executor the frame workers use.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from core import state

_registration_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="node-reg")


def register_node_blocking(node_id: str, config: dict) -> None:
    """Register with analytics and the associator. Callers on the event loop
    want `register_node`, not this."""
    state.node_analytics.register_node(node_id, config)
    state.node_associator.register_node(node_id, config)


async def register_node(node_id: str, config: dict) -> None:
    """Register a node from async code, off the event loop."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        _registration_executor,
        register_node_blocking,
        node_id,
        config,
    )
