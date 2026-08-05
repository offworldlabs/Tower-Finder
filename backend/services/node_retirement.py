"""Forget a node that has left the fleet.

Every per-node store in the server is append-only in practice.  Registration
adds; nothing removes.  A receiver that is decommissioned, renamed, or replaced
by a different fleet layout therefore keeps its analytics, coverage polygon,
custody chain and reputation for the life of the deployment, and every
subsequent analytics pass, snapshot write and coverage save keeps paying for
it.  Staging showed this directly after a layout change: 10 receivers that no
longer existed were still being iterated and re-saved to disk, and the node
count reported 26 against 16 live.

This is an explicit operation, not a staleness sweep.  A real receiver offline
for a week is still a real receiver whose accumulated coverage we want back
when it returns; only a decision that the node is *gone* should discard it.
``retire_node`` is that decision, and it is irreversible — the coverage
polygon in particular represents observation time that cannot be recreated.

Retiring a node that is currently connected is refused: it would be undone by
the next registration anyway, and it would strip the custody chain of a live
data source mid-session.
"""

import logging

from core import state

log = logging.getLogger(__name__)


class NodeStillConnected(Exception):
    """Raised when asked to retire a node that is currently active."""


def live_node_ids() -> set[str]:
    """Node IDs the server currently considers part of the fleet."""
    with state.connected_nodes_lock:
        return set(state.connected_nodes)


def stale_node_ids() -> list[str]:
    """Per-node state held for nodes that are not in the fleet.

    The union across every store, so a node that survives in only one of them
    (custody without analytics, say) is still reported.
    """
    live = live_node_ids()
    held: set[str] = set()
    analytics = getattr(state, "node_analytics", None)
    if analytics is not None:
        for store in (
            analytics.trust_scores, analytics.detection_areas, analytics.metrics,
            analytics.reputations, analytics.coverage_maps, analytics.empirical_coverages,
        ):
            held |= set(store)
    held |= set(state.node_identities)
    held |= set(state.chain_entries)
    held |= set(state.iq_commitments)
    return sorted(held - live)


def retire_node(node_id: str, *, force: bool = False) -> dict:
    """Drop every trace of ``node_id``: analytics, coverage files, custody.

    Returns a report of what was actually removed.  Raises NodeStillConnected
    if the node is active and ``force`` is not set.
    """
    if not force and node_id in live_node_ids():
        raise NodeStillConnected(node_id)

    report: dict = {"node_id": node_id}

    analytics = getattr(state, "node_analytics", None)
    if analytics is not None:
        report["analytics"] = analytics.retire_node(node_id)

    associator = getattr(state, "node_associator", None)
    if associator is not None and hasattr(associator, "unregister_node"):
        report["overlap_zones_removed"] = associator.unregister_node(node_id)

    report["custody"] = {
        "identity": state.node_identities.pop(node_id, None) is not None,
        "chain_entries": len(state.chain_entries.pop(node_id, []) or []),
        "iq_commitments": len(state.iq_commitments.pop(node_id, []) or []),
    }

    # The snapshot is written from these stores, so the next save already omits
    # the node; nothing needs to rewrite the file here.
    log.info("Retired node %s: %s", node_id, report)
    return report


def retire_stale_nodes() -> dict:
    """Retire every node held in state but absent from the fleet."""
    targets = stale_node_ids()
    return {
        "retired": [retire_node(nid) for nid in targets],
        "count": len(targets),
    }
