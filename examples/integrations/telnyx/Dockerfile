FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml README.md ./
COPY src/ src/
COPY examples/integrations/telnyx/ examples/integrations/telnyx/
RUN uv sync --project examples/integrations/telnyx --frozen --no-dev --no-editable

FROM python:3.12-slim
WORKDIR /app/examples/integrations/telnyx
COPY --from=build /app/examples/integrations/telnyx/.venv /app/examples/integrations/telnyx/.venv
COPY examples/integrations/telnyx/bridge.py examples/integrations/telnyx/instructions.md ./
ENV PATH="/app/examples/integrations/telnyx/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn bridge:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --ws-max-size 65536 --no-access-log --timeout-graceful-shutdown 30"]
