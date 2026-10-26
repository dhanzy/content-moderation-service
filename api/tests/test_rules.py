"""Unit tests for the decision rules. No database, no provider, no I/O."""
import pytest

from app.screening.rules import (
    BLOCK_THRESHOLD,
    HOLD_THRESHOLD,
    PROHIBITED_LABELS,
    decide,
    peak_scores,
)


def test_peak_scores_takes_the_max_across_all_frames():
    frames = [{"explicit": 0.1}, {"explicit": 0.9, "minor": 0.3}, {"minor": 0.7}]
    assert peak_scores(frames) == {"explicit": 0.9, "minor": 0.7}


def test_missing_label_scores_zero():
    assert peak_scores([{"explicit": 0.5}]).get("minor", 0.0) == 0.0


def test_non_numeric_scores_are_ignored_not_trusted():
    # A junk value must not become a pass or raise; it is simply not a score.
    assert peak_scores([{"minor": "very high"}]) == {}


@pytest.mark.parametrize("label", PROHIBITED_LABELS)
@pytest.mark.parametrize("verified", [True, False])
def test_prohibited_at_or_above_block_threshold_blocks_everywhere(label, verified):
    """Rule 2: 0.8 or more on a prohibited label blocks in every workspace."""
    decision = decide([{label: BLOCK_THRESHOLD}], verified)
    assert decision.status == "blocked"
    assert f"prohibited:{label}" in decision.reasons


@pytest.mark.parametrize("label", PROHIBITED_LABELS)
def test_prohibited_in_hold_band_holds_for_a_human(label):
    """Rule 2: 0.5 up to 0.8 holds for review."""
    decision = decide([{label: HOLD_THRESHOLD}], True)
    assert decision.status == "held"
    assert f"review:{label}" in decision.reasons


@pytest.mark.parametrize("label", PROHIBITED_LABELS)
def test_just_below_hold_threshold_publishes(label):
    assert decide([{label: 0.49}], True).status == "published"


def test_block_threshold_is_inclusive_and_hold_band_is_half_open():
    assert decide([{"minor": 0.8}], True).status == "blocked"
    assert decide([{"minor": 0.7999}], True).status == "held"
    assert decide([{"minor": 0.5}], True).status == "held"
    assert decide([{"minor": 0.4999}], True).status == "published"


def test_explicit_publishes_in_verified_workspace():
    """Rule 3: explicit content between consenting adults is allowed when verified."""
    decision = decide([{"explicit": 0.95}], True)
    assert decision.status == "published"
    assert decision.explicit is True


def test_explicit_is_blocked_in_unverified_workspace():
    decision = decide([{"explicit": 0.95}], False)
    assert decision.status == "blocked"
    assert decision.explicit is True
    assert "explicit_in_unverified_workspace" in decision.reasons


def test_explicit_below_threshold_is_not_explicit():
    decision = decide([{"explicit": 0.79}], False)
    assert decision.status == "published"
    assert decision.explicit is False


def test_blocking_beats_holding():
    """Most restrictive wins: a 0.9 blocks even though another label would hold."""
    decision = decide([{"minor": 0.9, "self_harm": 0.6}], True)
    assert decision.status == "blocked"
    assert decision.reasons == ["prohibited:minor"]


def test_prohibited_block_beats_explicit_rule():
    decision = decide([{"minor": 0.9, "explicit": 0.95}], True)
    assert decision.status == "blocked"
    assert "prohibited:minor" in decision.reasons


def test_several_prohibited_labels_are_all_reported():
    decision = decide([{"minor": 0.9}, {"bestiality": 0.85}], True)
    assert decision.reasons == ["prohibited:bestiality", "prohibited:minor"]


def test_one_bad_frame_anywhere_in_a_long_video_is_enough():
    """Rule 9: every frame of a video counts, not just the beginning."""
    frames = [{"explicit": 0.01}] * 200
    frames[173] = {"minor": 0.9}
    assert decide(frames, True).status == "blocked"


def test_empty_frames_are_held_not_published():
    """Nothing to go on is not the same as nothing wrong.

    Guarded upstream too -- the API rejects a frameless payload and the provider
    client treats a short result as malformed -- but the rules themselves must
    not publish on an empty score set.
    """
    decision = decide([], True)
    assert decision.status == "held"
    assert decision.reasons == ["screening_unavailable:no_frames"]
