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


def evict_pipeline(node_id: str) -> None:
    """Drop a node's cached PassiveRadarPipeline so the next frame rebuilds it.

    services.frame_processor.get_or_create_node_pipeline builds a node's
    pipeline from its config once and caches it in state.node_pipelines
    indefinitely; nothing else refreshes it (the only other eviction is
    services.tasks.analytics_refresh's disconnect-TTL sweep, which is a
    different mechanism for a different problem). Call this wherever a node's
    active configuration is replaced — services.node_pipeline.register_with_pipeline
    (the v1 PUT /v1/nodes/config path) and the TCP CONFIG handshake in
    services.tcp_handler both do — so a corrected antenna aim or beam width
    reaches the map arcs on the next frame instead of only after a restart.

    A pop rather than an in-place edit of the cached pipeline: a frame worker
    may already hold a reference to it for a frame it is mid-way through
    processing, and finishing that frame against the old geometry is fine —
    only the *next* frame needs the new config, and popping leaves whatever
    the worker is holding untouched.

    Safe to call with nothing cached, which is the ordinary case for a fresh
    registration: pop with a default is a no-op, and
    get_or_create_node_pipeline builds that node's first pipeline from the
    config just written, exactly as before this existed.
    """
    state.node_pipelines.pop(node_id, None)
