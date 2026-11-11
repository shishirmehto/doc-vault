FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.6 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY docvault ./docvault
RUN uv sync --frozen --no-dev
RUN useradd --create-home vault && mkdir -p /data/blobs && chown vault /data/blobs
USER vault
