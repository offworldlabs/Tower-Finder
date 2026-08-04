"""The solver-side track claim — the selection that actually runs.

Production builds the associator with cv_fit=None, so its stage-2 hypothesis
selection never executes; _claim_track_pair is what arbitrates competing
explanations of a single-node track.  It had no tests.

The behaviour that needs pinning is not the competitive case, which is
straightforward, but what happens to a pairing across rounds: chi2 is refitted
every round over a growing epoch set, so the score of a *stable, winning*
pairing drifts.  The held score is a best-ever high-water mark, so upward drift
loses to the pairing's own earlier claim.

That reads as a bug and was investigated as one.  It is not: measured blind on
6 paired seeds, letting a pairing renew its own claim is never better and worse
on 3 of 6, costing 2.7 points of track ghost rate and 4.2 at n=2-only for no
gain in real tracks — the renewal raises the bar competitors must beat, tracks
change hands more, and each hand-over publishes another track.  These tests
pin the hysteresis deliberately.  See _claim_track_pair's docstring.
"""

import os
import time

import pytest

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from services.tasks import solver  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_claims():
    solver._TRACK_CLAIMS.clear()
    yield
    solver._TRACK_CLAIMS.clear()


def _pairing(a="a1", b="b1", extra_tracks=()):
    return {
        "track_pair_ids": [(a, b)],
        "track_ids": sorted({a, b, *extra_tracks}),
        "n_nodes": 2,
    }


class TestCompetition:
    def test_an_unclaimed_pairing_wins(self):
        assert solver._claim_track_pair(_pairing(), 0.5) is True

    def test_a_worse_competitor_is_refused(self):
        """One track is one aircraft, so two explanations of it are mutually
        exclusive and the better fit should hold it."""
        assert solver._claim_track_pair(_pairing("a1", "b1"), 0.5) is True
        assert solver._claim_track_pair(_pairing("a1", "c1"), 1.5) is False

    def test_a_better_competitor_takes_the_track(self):
        assert solver._claim_track_pair(_pairing("a1", "b1"), 1.5) is True
        assert solver._claim_track_pair(_pairing("a1", "c1"), 0.5) is True

    def test_disjoint_pairings_do_not_contend(self):
        assert solver._claim_track_pair(_pairing("a1", "b1"), 0.5) is True
        assert solver._claim_track_pair(_pairing("c1", "d1"), 1.5) is True

    def test_a_detection_level_input_is_not_arbitrated(self):
        assert solver._claim_track_pair({"n_nodes": 2}, 9.9) is True


class TestSelfContention:
    def test_an_identical_rescore_keeps_its_claim(self):
        """Documented and working: the strictly-better test exists so a pairing
        re-solving itself does not lose to its own previous score."""
        assert solver._claim_track_pair(_pairing(), 0.5) is True
        assert solver._claim_track_pair(_pairing(), 0.5) is True

    def test_a_drifting_rescore_loses_to_its_own_high_water_mark(self):
        """Deliberate hysteresis, not an oversight.

        chi2 is refitted every association round over a growing epoch set, so a
        stable winning pairing's score wanders — a median +0.09 to +0.18
        between rounds on the bench.  A pairing must keep fitting at least as
        well as its own best to stay published, because one whose fit is
        degrading is one whose accumulated evidence is turning against it.

        Measured alternative (association_bench.py --claim-policy self-refresh)
        is never better across 6 paired seeds and worse on 3.
        """
        assert solver._claim_track_pair(_pairing(), 0.50) is True
        assert solver._claim_track_pair(_pairing(), 0.68) is False

    def test_the_high_water_mark_is_not_reset_by_a_refusal(self):
        """A refusal does not refresh the holder — that is what makes it a
        high-water mark rather than a comparison against the current score."""
        solver._claim_track_pair(_pairing(), 0.50)
        for _ in range(5):
            assert solver._claim_track_pair(_pairing(), 0.68) is False
        held_chi2, _ = solver._TRACK_CLAIMS["a1"]
        assert held_chi2 == 0.50


class TestExpiry:
    def test_a_claim_expires(self):
        solver._claim_track_pair(_pairing("a1", "b1"), 0.1)
        stale = time.time() - solver._TRACK_CLAIM_TTL_S - 1
        solver._TRACK_CLAIMS["a1"] = (0.1, stale)
        solver._TRACK_CLAIMS["b1"] = (0.1, stale)
        assert solver._claim_track_pair(_pairing("a1", "c1"), 5.0) is True

    def test_expiry_sweeps_every_stale_entry(self):
        for i in range(20):
            solver._TRACK_CLAIMS[f"t{i}"] = (0.1, time.time() - 1000)
        solver._claim_track_pair(_pairing("z1", "z2"), 0.5)
        assert not [k for k in solver._TRACK_CLAIMS if k.startswith("t")]


class TestClusterCoverage:
    def test_only_the_first_pair_of_a_cluster_is_claimed(self):
        """format_track_pairs_for_solver truncates track_pair_ids to [:1], so a
        cluster spanning three tracks leaves the third unclaimed and free to
        seed a competing target.  Pinned as current behaviour, not endorsed —
        see the claim-policy sweep in association_bench.py."""
        solver._claim_track_pair(_pairing("a1", "b1", extra_tracks=("c1",)), 0.5)
        assert "a1" in solver._TRACK_CLAIMS
        assert "b1" in solver._TRACK_CLAIMS
        assert "c1" not in solver._TRACK_CLAIMS
