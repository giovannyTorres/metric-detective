FROM ghcr.io/astral-sh/uv:0.13.5 AS uv
FROM python:3.13.5-slim
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY data ./data
COPY examples ./examples
COPY .streamlit ./.streamlit
RUN uv sync --locked --no-dev && useradd --create-home --uid 10001 detective && mkdir -p /app/runtime/datasets && chown -R detective:detective /app/runtime
USER detective
EXPOSE 8000 8501
CMD ["uvicorn", "metric_detective.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
