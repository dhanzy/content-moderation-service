import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import init_db
from app.logging_conf import log_event, setup_logging
from app.routes import callback, internal, media, review

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    await init_db()
    if settings.broken:
        log_event(
            log,
            "broken_mode_active",
            level=logging.ERROR,
            note="SCREENING_MODE=broken: this build reproduces the incident on purpose",
        )
    log_event(log, "api_start", mode=settings.screening_mode)
    yield


app = FastAPI(title="content-moderation-api", lifespan=lifespan)

# The one-page UI is served from a different origin in development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(media.router)
app.include_router(review.router)
app.include_router(callback.router)
app.include_router(internal.router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "screening_mode": settings.screening_mode}
