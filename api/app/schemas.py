from typing import Literal

from pydantic import BaseModel, Field, field_validator


class MediaIn(BaseModel):
    """The media JSON: frames plus the scores the provider will return."""

    type: Literal["image", "video"]
    source: Literal["upload", "generation"]
    prompt: str | None = None
    frames: list[dict[str, float]] = Field(default_factory=list)

    @field_validator("frames")
    @classmethod
    def frames_present(cls, value: list[dict[str, float]]) -> list[dict[str, float]]:
        if not value:
            raise ValueError("at least one frame is required")
        return value


class MediaAccepted(BaseModel):
    id: str
    status: str


class MediaOut(BaseModel):
    id: str
    status: str
    reasons: list[str]
    type: str
    source: str
    explicit: bool
    frames_total: int
    frames_scored: int
    screened: bool
    created_at: str
