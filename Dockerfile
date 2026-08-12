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

# Tier 2 is opt-in at BUILD time, because torch and gliner add roughly a
# gigabyte and the processor runs rules-only without them. Build with
#   docker build --build-arg INSTALL_TIER2=true .
# or set INSTALL_TIER2=true in .env for docker compose.
#
# Setting TIER2_ENABLED=true at RUN time on an image built without this now
# fails with an instruction rather than a bare ModuleNotFoundError -- see
# processor._load_tier2.
ARG INSTALL_TIER2=false

# Dependency metadata and sources are copied before the rest so that editing
# a script or a SQL file does not invalidate the pip layer.
COPY pyproject.toml ./
COPY src/ ./src/
# torch comes from the CPU index explicitly. pip's default index serves the
# CUDA build on Linux, which is several gigabytes of driver payload this image
# can never use -- there is no GPU in it.
RUN if [ "$INSTALL_TIER2" = "true" ]; then \
        pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
            "torch>=2.13" && \
        pip install --no-cache-dir ".[tier2]"; \
    else \
        pip install --no-cache-dir .; \
    fi

COPY scripts/ ./scripts/
COPY db/ ./db/

# Non-root. The audit trail is in Postgres and the report goes to stdout, so
# the only thing that ever writes to disk is the model cache below.
RUN useradd --create-home --uid 1000 app && chown -R app:app /app

# Weights land here, and HF_HOME points at it so the location is one the
# compose file can mount a volume over. Without a volume the container
# re-downloads ~1.7 GB on every start, which turns `restart: unless-stopped`
# plus a network blip into a crash loop.
ENV HF_HOME=/models
RUN mkdir -p /models && chown app:app /models

USER app

# Overridden by the topics-init, producer and report services in compose.
CMD ["python", "-m", "pipelineguard.processor"]
