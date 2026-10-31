FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock PYPI_README.md LICENSE ./
RUN uv sync --frozen --extra hosted --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --extra hosted --no-dev && mkdir /data && chown 10001:10001 /data
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
VOLUME /data
USER 10001:10001
CMD ["python", "-m", "openinvest.hosted.worker"]
