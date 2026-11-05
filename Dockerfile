# Pin PYTHON_IMAGE to a reviewed digest for a specific production release.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TOWER_DATABASE=/app/data/tower.sqlite3 \
    TOWER_LOG_DIR=/app/logs

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --requirement requirements.txt \
    && python -m pip check \
    && groupadd --gid 10001 tower \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin tower \
    && mkdir -p /app/data /app/logs \
    && chown 10001:10001 /app/data /app/logs

COPY tower ./tower
COPY scripts/*.py ./scripts/
USER 10001:10001

HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD ["python", "-m", "tower", "health"]

ENTRYPOINT ["python", "-m", "tower"]
CMD ["run"]
