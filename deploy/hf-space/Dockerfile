# The public demo image, for a Hugging Face Space on the Docker SDK.
#
# Docker SDK rather than the Streamlit SDK for one reason: a build step. GLiNER
# resolves its backbone config from microsoft/deberta-v3-base at `main` and
# passes no revision, so the only way to pin it is to put exactly the right
# commit in the cache and then forbid the network (findings section 25). That
# needs `python -m pipelineguard.prefetch` to run at build time, which the
# Streamlit SDK gives no place to do. Without it the demo would run unpinned
# and its own sidebar would say so.
#
# Build context is the repo root:
#     docker build -f deploy/hf-space/Dockerfile -t pipelineguard-demo .

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/

# torch from the CPU index explicitly. The default index serves the CUDA build
# on Linux, which is gigabytes of driver payload for a machine with no GPU.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.13" \
 && pip install --no-cache-dir ".[tier2,ui]"

# The stored governance run the demo renders, since there is no database here.
COPY docs/sample-summary.md ./docs/
COPY .streamlit/ ./.streamlit/

# Weights are baked into the image rather than fetched on first boot: a Space
# that downloads 1.6 GB while someone is watching is a broken first impression,
# and an unpinned backbone is worse.
ENV HF_HOME=/models \
    HF_HUB_DISABLE_XET=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    HF_HUB_DISABLE_TELEMETRY=1
ARG TIER2_MODEL_REVISION=88c3b98b57ad5e7d66fb209ed61c53f4b1fd05da
ENV TIER2_MODEL_REVISION=${TIER2_MODEL_REVISION}
RUN mkdir -p /models && python -m pipelineguard.prefetch

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

# --server.address overrides .streamlit/config.toml, which binds loopback so a
# local run cannot be reached off the machine. That is the deliberate exception:
# a container has to bind 0.0.0.0 to be reachable at all, and PG_DEMO=1 above
# is what stops this build claiming to be the local-only one.
CMD ["streamlit", "run", "src/pipelineguard/ui.py", \
     "--server.port", "7860", \
     "--server.address", "0.0.0.0", \
     "--server.headless", "true", \
     "--browser.gatherUsageStats", "false"]
