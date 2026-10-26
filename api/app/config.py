import os
from pathlib import Path

from dotenv import load_dotenv

# Load api/.env, if it exists, so the services can be started from a shell with
# nothing exported.
#
# override=False is the important part: a variable already present in the real
# environment always wins. Under Compose every setting arrives that way, so a
# stray .env can never quietly shadow what docker-compose.yml says -- the file
# only fills in what nobody set. The path is explicit rather than discovered by
# walking up from the working directory, so `uv run` from api/ and
# `python -m app.workq.worker` from anywhere resolve the same file.
#
# .env is gitignored; .env.example is the committed template.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


class Settings:
    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://mod:mod@localhost:5432/moderation"
    )
    redis_url: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    provider_url: str = os.environ.get("PROVIDER_URL", "http://localhost:8001")
    provider_secret: bytes = os.environ.get("PROVIDER_SECRET", "dev-provider-secret").encode()

    # "fixed" is the real handler. "broken" is the deliberately broken handler kept
    # in the tree so the incident can be reproduced on demand; see README.
    screening_mode: str = os.environ.get("SCREENING_MODE", "fixed")

    max_attempts: int = int(os.environ.get("MAX_ATTEMPTS", "5"))
    lease_seconds: int = int(os.environ.get("LEASE_SECONDS", "30"))
    worker_concurrency: int = int(os.environ.get("WORKER_CONCURRENCY", "4"))

    provider_timeout_s: float = float(os.environ.get("PROVIDER_TIMEOUT_S", "5.0"))
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")

    # Fault injection. When set to a media ID (or "*"), the worker hard-exits the
    # instant the provider has scored that item and before the result is saved,
    # to exercise the one window where a call can be paid for twice.
    crash_after_score: str = os.environ.get("CRASH_AFTER_SCORE", "")

    # Fault injection for the review queue, so the loading / failed / empty
    # states can be exercised against a real failure instead of a mock.
    review_fail: bool = os.environ.get("REVIEW_FAIL", "") not in ("", "0", "false")

    @property
    def broken(self) -> bool:
        return self.screening_mode == "broken"


settings = Settings()
