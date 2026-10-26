"""JSON logging keyed on workspace / media / job / attempt.

Every screening log line carries those four fields where they are known, so one
media item can be followed from upload through a failure and recovery with:

    docker compose logs worker api | grep '"media_id": "<id>"'
"""
import json
import logging
import sys
from datetime import datetime, timezone

from app.config import settings

CONTEXT_KEYS = ("workspace_id", "media_id", "job_id", "attempt_no", "event")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in CONTEXT_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    logging.getLogger("uvicorn.access").setLevel("WARNING")
    # The reconciler probes the provider every few seconds; at INFO this buries
    # the screening events that actually matter when following a media item.
    logging.getLogger("httpx").setLevel("WARNING")
    logging.getLogger("httpcore").setLevel("WARNING")


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    workspace_id: str | None = None,
    media_id: str | None = None,
    job_id: str | None = None,
    attempt_no: int | None = None,
    **fields,
) -> None:
    logger.log(
        level,
        event,
        extra={
            "event": event,
            "workspace_id": workspace_id,
            "media_id": media_id,
            "job_id": job_id,
            "attempt_no": attempt_no,
            "extra_fields": fields,
        },
    )
