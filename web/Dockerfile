FROM oven/bun:1.3.13-slim

WORKDIR /srv

# Dependencies resolve from bun.lock only (--frozen-lockfile), so the image gets
# exactly the versions committed to the repo and the build fails rather than
# silently re-resolving if the lock is stale.
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile

COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src

EXPOSE 5173

# The Vite dev server, not a production build behind a real web server. Two
# reasons: VITE_API_BASE is baked in at build time, so a prebuilt bundle could
# not be pointed at a different API by compose, and this is a one-page internal
# tool for an exercise. A real deployment would build and serve the static
# output. Noted as a limitation in the README.
CMD ["bun", "run", "dev"]
