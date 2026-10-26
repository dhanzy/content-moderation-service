"""The decision rules, as one pure function over a list of scored frames.

Kept free of I/O so it can be tested exhaustively and so both the fixed and the
broken handler reach their verdict through the same code path. The only thing
the broken handler does differently is lie about which frames it looked at.
"""
from dataclasses import dataclass, field

# Blocked for every account, verified or not, on upload and on generation.
PROHIBITED_LABELS = (
    "minor",
    "sexual_violence",
    "bestiality",
    "self_harm",
    "real_person",
)

BLOCK_THRESHOLD = 0.8
HOLD_THRESHOLD = 0.5
EXPLICIT_THRESHOLD = 0.8

BLOCKED = "blocked"
HELD = "held"
PUBLISHED = "published"
PENDING = "pending"


@dataclass
class Decision:
    status: str
    explicit: bool = False
    reasons: list[str] = field(default_factory=list)


def peak_scores(frames: list[dict]) -> dict[str, float]:
    """Highest score each label reaches across *every* frame.

    A video is as unsafe as its worst second. The incident video was explicit
    only in its last minute, so taking the max over all frames -- rather than
    over a prefix -- is the whole point.
    """
    peaks: dict[str, float] = {}
    for frame in frames or []:
        for label, score in (frame or {}).items():
            try:
                value = float(score)
            except (TypeError, ValueError):
                continue
            if value > peaks.get(label, 0.0):
                peaks[label] = value
    return peaks


def decide(frames: list[dict], workspace_verified: bool) -> Decision:
    """Reach a verdict for a media item from its scored frames.

    Rule order is most-restrictive-first, so a prohibited label at 0.9 blocks an
    item even if another rule would merely have held it.
    """
    # No frames means nothing was scored, which is not the same as nothing being
    # wrong. Fail closed: hold it. The API rejects a frameless payload and the
    # provider client treats a short result as malformed, so this is the third
    # layer rather than the first -- but it is the one that cannot be bypassed.
    if not frames:
        return Decision(status=HELD, reasons=["screening_unavailable:no_frames"])

    peaks = peak_scores(frames)
    reasons: list[str] = []

    # Rule 2: a prohibited label at or above 0.8 blocks the media everywhere.
    hard = [
        label
        for label in PROHIBITED_LABELS
        if peaks.get(label, 0.0) >= BLOCK_THRESHOLD
    ]
    if hard:
        return Decision(
            status=BLOCKED,
            explicit=peaks.get("explicit", 0.0) >= EXPLICIT_THRESHOLD,
            reasons=[f"prohibited:{label}" for label in sorted(hard)],
        )

    # Rule 3: explicit at or above 0.8 marks the item explicit. Only verified
    # workspaces may publish it; in an unverified workspace it is blocked.
    explicit = peaks.get("explicit", 0.0) >= EXPLICIT_THRESHOLD
    if explicit and not workspace_verified:
        return Decision(
            status=BLOCKED,
            explicit=True,
            reasons=["explicit_in_unverified_workspace"],
        )

    # Rule 2, second half: 0.5 up to 0.8 on a prohibited label holds it for a human.
    soft = [
        label
        for label in PROHIBITED_LABELS
        if HOLD_THRESHOLD <= peaks.get(label, 0.0) < BLOCK_THRESHOLD
    ]
    if soft:
        return Decision(
            status=HELD,
            explicit=explicit,
            reasons=[f"review:{label}" for label in sorted(soft)],
        )

    return Decision(status=PUBLISHED, explicit=explicit, reasons=reasons)


def held_for_no_decision(error_class: str) -> Decision:
    """Rule 5: with no decision available the item is held, never published."""
    return Decision(status=HELD, explicit=False, reasons=[f"screening_unavailable:{error_class}"])
