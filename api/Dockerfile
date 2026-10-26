# The uv binary is copied from its own published image rather than pip-installed,
# so the build does not depend on an index resolution for its own tooling.
FROM ghcr.io/astral-sh/uv:0.8.6 AS uv

FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/srv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/srv/.venv \
    PATH="/srv/.venv/bin:$PATH"

# Dependencies resolve from uv.lock only (--frozen), so the image gets exactly
# the versions committed to the repo. Dev dependencies are included because the
# regression suite runs inside this image: docker compose exec api pytest.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen

COPY app ./app
COPY tests ./tests

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
