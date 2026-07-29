# One image, four entry points: the processor (default), the topic creator,
# the producer and the governance report. They share every dependency, so
# building four images would cost four layers of the same thing.

# 3.12 rather than the host's 3.14: confluent-kafka and psycopg[binary] both
# ship manylinux wheels for 3.12, which is what keeps this image free of a
# compiler and of a separate librdkafka install. The container has no reason
# to match the host interpreter.
FROM python:3.12-slim

# Unbuffered so `docker compose logs -f processor` shows the stats line as it
# is emitted rather than when the block buffer happens to fill. A pipeline
# whose observability arrives in bursts is worse than one with none, because
# the delay reads as a stall.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency metadata and sources are copied before the rest so that editing
# a script or a SQL file does not invalidate the pip layer.
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

COPY scripts/ ./scripts/
COPY db/ ./db/

# Non-root. Nothing here writes to the filesystem in normal operation --
# the audit trail is in Postgres and the report goes to stdout -- so there is
# no reason to run as root.
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

# Overridden by the topics-init, producer and report services in compose.
CMD ["python", "-m", "pipelineguard.processor"]
