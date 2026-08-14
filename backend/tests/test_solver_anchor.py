"""Tests for top-down claiming's solver-side integration.

Two things are pinned here:

- multinode_key_decision, the pure keying rule extracted from the old
  _multinode_track_key (precedent: claim_decision) — adsb precedence,
  anchor honoring (and its distance/liveness/dark-only guards), the
  existing proximity scan, and minting.
- Its threading through _process_solver_item: an anchor_key on the solver
  input drives publication onto the anchor's own key, bumps the
  solver_anchor_* / solver_anchored_published counters, is carried into
  mlat_solve_history, and — the regression pin — an anchored n=2 input
  with no cv_epochs is withheld exactly like any other unfitted n=2
  pairing, never publishing under a phantom fit.

Style follows test_solver_consensus.py / test_solver_worker.py /
test_track_claim.py.
"""

import time

from core import state
from services.tasks import solver as solver_mod

LAT, LON = 35.0, -82.0

# An n=2 solver input whose track pairing has already passed the
# constant-velocity fit — same shape as test_solver_worker.py's
# _CONFIRMED_N2, so the n=2 confirmation gate is a non-issue for the tests
# that are not specifically about it.
_CONFIRMED_N2 = {"n_nodes": 2, "chi2_per_dof": 0.5, "n_epochs": 8}


def _reset():
    state._reset_for_tests()
    solver_mod._reset_for_tests()


def _anchor_track(**overrides) -> dict:
    """A live mn-dark-* entry in state.multinode_tracks, at (LAT, LON)."""
    rec = {
        "lat": LAT,
        "lon": LON,
        "vel_east": 0.0,
        "vel_north": 0.0,
        "timestamp_ms": int(time.time() * 1000) - 5000,
        "n_nodes": 2,
        "solve_count": 2,
    }
    rec.update(overrides)
    return rec


def _solve_fn(lat=LAT, lon=LON, ts_ms=None, node_ids=("n1", "n2")):
    def fn(s_in, cfgs):
        return {
            "success": True,
            "lat": lat,
            "lon": lon,
            "timestamp_ms": ts_ms if ts_ms is not None else int(time.time() * 1000),
            "contributing_node_ids": list(node_ids),
        }

    return fn


class TestMultinodeKeyDecision:
    """The pure rule, units — same style as claim_decision's TestCompetition
    etc. in test_track_claim.py."""

    def test_adsb_takes_precedence_over_an_anchor(self):
        tracks = {"mn-dark-1": _anchor_track()}
        key, how = solver_mod.multinode_key_decision(
            tracks,
            {"lat": LAT, "lon": LON, "timestamp_ms": 1000},
            "abc123",
            "mn-dark-1",
        )
        assert (key, how) == ("mn-adsb-abc123", "adsb")

    def test_anchor_hit_when_close_and_still_live(self):
        tracks = {"mn-dark-anchor": _anchor_track()}
        # ~1.1 km away — comfortably inside the 6 km default.
        key, how = solver_mod.multinode_key_decision(
            tracks,
            {"lat": LAT + 0.01, "lon": LON, "timestamp_ms": 1000},
            None,
            "mn-dark-anchor",
        )
        assert (key, how) == ("mn-dark-anchor", "anchor")

    def test_missing_anchor_key_falls_back_to_mint(self):
        """anchor_key points at nothing live, and there is nothing else to
        fall back to by proximity either — mints, same as no anchor at all."""
        key, how = solver_mod.multinode_key_decision(
            {},
            {"lat": LAT, "lon": LON, "timestamp_ms": 1000},
            None,
            "mn-dark-does-not-exist",
        )
        assert how == "minted"
        assert key.startswith("mn-dark-1000-")

    def test_anchor_beyond_max_dist_km_falls_back(self):
        tracks = {"mn-dark-anchor": _anchor_track()}
        # ~11 km away — past the 6 km default max_dist_km.  The consensus-
        # anchored-displacement edge case this distance check exists for.
        key, how = solver_mod.multinode_key_decision(
            tracks,
            {"lat": LAT + 0.1, "lon": LON, "timestamp_ms": 1000},
            None,
            "mn-dark-anchor",
        )
        assert how != "anchor"
        assert key != "mn-dark-anchor"

    def test_non_dark_anchor_key_is_refused(self):
        """An anchor_key must start mn-dark-; a caller passing an ADS-B key
        (should never happen — only dark tracks are claim_eligible — but the
        rule itself must refuse it, not trust the caller)."""
        tracks = {"mn-adsb-abc": _anchor_track()}
        key, how = solver_mod.multinode_key_decision(
            tracks,
            {"lat": LAT, "lon": LON, "timestamp_ms": 1000},
            None,
            "mn-adsb-abc",
        )
        assert how != "anchor"

    def test_mints_a_new_key_with_no_anchor_and_no_claimant(self):
        key, how = solver_mod.multinode_key_decision(
            {},
            {"lat": LAT, "lon": LON, "timestamp_ms": 1000},
            None,
            None,
        )
        assert how == "minted"
        assert key.startswith("mn-dark-1000-")


class TestProcessSolverItemAnchorHonoring:
    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def test_anchored_publish_lands_on_the_anchor_key(self):
        state.multinode_tracks["mn-dark-anchor-1"] = _anchor_track()
        s_in = dict(_CONFIRMED_N2, anchor_key="mn-dark-anchor-1")
        item = (s_in, {}, time.time())

        result = solver_mod._process_solver_item(item, _solve_fn())

        assert result is not None
        assert "mn-dark-anchor-1" in state.multinode_tracks
        assert state.solver_anchored_published == 1
        assert state.solver_anchor_hits == 1
        assert state.solver_anchor_fallbacks == 0

    def test_fallback_still_publishes_and_counts_as_a_fallback(self):
        """Anchor proposed but too far from THIS solve's own result — falls
        through to mint, still publishes, and is attributed as a fallback
        rather than a hit."""
        state.multinode_tracks["mn-dark-anchor-2"] = _anchor_track()
        s_in = dict(_CONFIRMED_N2, anchor_key="mn-dark-anchor-2")
        item = (s_in, {}, time.time())

        result = solver_mod._process_solver_item(item, _solve_fn(lat=LAT + 0.5, lon=LON))  # ~55 km away

        assert result is not None
        assert state.solver_anchored_published == 1
        assert state.solver_anchor_hits == 0
        assert state.solver_anchor_fallbacks == 1

    def test_history_carries_the_anchor_key(self):
        state.multinode_tracks["mn-dark-anchor-3"] = _anchor_track()
        s_in = dict(_CONFIRMED_N2, anchor_key="mn-dark-anchor-3")
        item = (s_in, {}, time.time())

        solver_mod._process_solver_item(item, _solve_fn())

        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "published"
        assert rec["anchor_key"] == "mn-dark-anchor-3"

    def test_anchor_key_is_recorded_on_rejects_too(self):
        """Windowed fragmentation needs the field on every attempt, not just
        successful publishes — solver.py stamps it unconditionally, before
        any gate has a chance to reject."""
        state.multinode_tracks["mn-dark-anchor-4"] = _anchor_track()
        s_in = dict(_CONFIRMED_N2, anchor_key="mn-dark-anchor-4")
        item = (s_in, {}, time.time())

        def unconverged_solve_fn(s_in, cfgs):
            return {"success": False}  # LM ran, did not converge

        solver_mod._process_solver_item(item, unconverged_solve_fn)

        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "unconverged"
        assert rec["anchor_key"] == "mn-dark-anchor-4"

    def test_supersession_merges_a_proximity_minted_fragment_into_the_anchor(self):
        """A fragment sharing the anchor's source tracks, minted earlier
        under a different key (bottom-up path, missed the 6 km match), is
        absorbed into the anchor when the anchor's own solve arrives —
        never the reverse: the anchor's key is what survives."""
        state.multinode_tracks["mn-dark-anchor-5"] = _anchor_track(source_track_ids=["t1", "t2"])
        state.multinode_tracks["mn-dark-fragment"] = _anchor_track(
            lat=LAT + 1.0, solve_count=1, source_track_ids=["t1", "t2"]
        )

        s_in = dict(_CONFIRMED_N2, anchor_key="mn-dark-anchor-5", track_ids=["t1", "t2"])
        item = (s_in, {}, time.time())
        solver_mod._process_solver_item(item, _solve_fn())

        assert "mn-dark-anchor-5" in state.multinode_tracks
        assert "mn-dark-fragment" not in state.multinode_tracks
        assert state.mn_superseded == 1
        # solve_count carries forward through the merge.
        assert state.multinode_tracks["mn-dark-anchor-5"]["solve_count"] == 3

    def test_anchored_n2_without_cv_epochs_is_withheld(self):
        """Regression pin: an anchored n=2 input MUST carry cv_epochs (built
        by _merge_epochs_multi) or it dies at this gate forever — without
        them _resolve_cv_fit has nothing to fit, chi2_per_dof never
        resolves, and the n=2 confirmation gate withholds it exactly like
        any other unfitted pairing.  It must NOT publish just because it
        has an anchor_key."""
        state.multinode_tracks["mn-dark-anchor-6"] = _anchor_track()
        s_in = {"n_nodes": 2, "anchor_key": "mn-dark-anchor-6"}  # no cv_epochs
        item = (s_in, {}, time.time())

        result = solver_mod._process_solver_item(item, _solve_fn())

        assert result is not None  # the solve itself ran
        assert state.n2_unconfirmed == 1
        assert state.solver_anchored_published == 0
        assert state.solver_anchor_hits == 0
        # The pre-existing anchor entry is untouched — nothing was written.
        assert state.multinode_tracks["mn-dark-anchor-6"]["solve_count"] == 2
