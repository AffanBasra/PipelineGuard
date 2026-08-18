# The public demo image, for any Docker host with more than 1 GB of memory.
#
# The advantage over the Streamlit Community Cloud build in
# deploy/streamlit-cloud/ is the build step. GLiNER resolves its backbone config
# from microsoft/deberta-v3-base at `main` and passes no revision, so the only
# way to pin it is to put exactly the right commit in the cache and then forbid
# the network (findings section 25). Here that happens once at build time and
# the weights are baked in; there it has to happen at every cold boot.
#
# This image runs fp32, unlike the memory-constrained build.
#
# Build context is the repo root:
#     docker build -f deploy/hf-space/Dockerfile -t pipelineguard-demo .

FROM python:3.12-slim

# PIP_DEFAULT_TIMEOUT: the CPU torch wheel is ~200 MB and pip's 15 s default
# read timeout fails the whole build on a slow link. Twice observed here.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

WORKDIR /app

COPY pyproject.toml ./

# Only the modules `python -m pipelineguard.prefetch` imports, and deliberately
# not the rest. The download below is a 1.6 GB layer, so anything copied above
# it re-downloads the weights when it changes -- with the whole tree here, one
# edit to ui.py cost a full re-fetch.
COPY src/pipelineguard/__init__.py \
     src/pipelineguard/config.py \
     src/pipelineguard/models.py \
     src/pipelineguard/observability.py \
     src/pipelineguard/prefetch.py \
     ./src/pipelineguard/
COPY src/pipelineguard/detectors/__init__.py \
     src/pipelineguard/detectors/tier2_encoder.py \
     ./src/pipelineguard/detectors/

# torch from the CPU index explicitly. The default index serves the CUDA build
# on Linux, which is gigabytes of driver payload for a machine with no GPU.
#
# Editable, so the full `COPY src/` below lands without a reinstall. A
# non-editable install copies into site-packages and the later files would be
# ignored, which is the trap this ordering would otherwise walk into.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.13" \
 && pip install --no-cache-dir -e ".[tier2,ui]"

# Weights are baked into the image rather than fetched on first boot: a host
# that downloads 1.6 GB while someone is watching is a broken first impression,
# and an unpinned backbone is worse.
ENV HF_HOME=/models \
    HF_HUB_DISABLE_XET=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    HF_HUB_DISABLE_TELEMETRY=1
ARG TIER2_MODEL_REVISION=88c3b98b57ad5e7d66fb209ed61c53f4b1fd05da
ENV TIER2_MODEL_REVISION=${TIER2_MODEL_REVISION}
RUN mkdir -p /models && python -m pipelineguard.prefetch

# Everything the app needs but the prefetch does not. Below the download on
# purpose: this is the layer that changes on every code edit.
COPY src/ ./src/
# The stored governance run the demo renders, since there is no database here.
COPY docs/sample-summary.md ./docs/
COPY .streamlit/ ./.streamlit/

# Spaces run the container as uid 1000.
RUN useradd --create-home --uid 1000 app && chown -R app:app /app /models
USER app

# Offline from here on, which is what makes the pin above exact rather than
# merely likely: offline resolution can only return what is already cached.
ENV HF_HUB_OFFLINE=1 \
    PG_DEMO=1 \
    TOKENIZERS_PARALLELISM=false \
    TRANSFORMERS_VERBOSITY=error

EXPOSE 7860

# 0.0.0.0 because a container has to bind every interface to be reachable at
# all. That is the deliberate exception to the local-only rule, and PG_DEMO=1
# above is what stops this build claiming to be the local one.
CMD ["streamlit", "run", "src/pipelineguard/ui.py", \
     "--server.port", "7860", \
     "--server.address", "0.0.0.0", \
     "--server.headless", "true", \
     "--browser.gatherUsageStats", "false"]
