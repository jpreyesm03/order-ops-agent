# API image (FastAPI + agent). Multi-stage: dependencies are built once, the runtime image has no build tools.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra aws --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra aws

FROM python:3.12-slim
# On AWS Lambda this extension turns each invocation into an HTTP request to uvicorn below (a Function URL request,
# or a POST /events for async and scheduled invocations). Anywhere else it is inert and the image runs as is.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src
COPY data ./data
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080 \
    AWS_LWA_PORT=8080 AWS_LWA_READINESS_CHECK_PATH=/healthz
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"
CMD ["sh", "-c", "uvicorn order_ops.api:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
