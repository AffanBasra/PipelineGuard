"""Entrypoint for the public demo on Streamlit Community Cloud.

Community Cloud has no build step: it installs a requirements file and runs one
script. The Docker demo does two things at build time that the app cannot do
without, so they happen here instead, at boot:

  1. fetch exactly the pinned commits of both HuggingFace repos (findings §25),
  2. then forbid the network, which is what makes that pin exact rather than
     merely likely.

Point Community Cloud at THIS file, not at src/pipelineguard/ui.py. The
requirements file beside it is the one Community Cloud picks up, and this is
also the only place a Community Cloud deployment can be configured -- it offers
no way to set environment variables.
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_UI = _REPO / "src" / "pipelineguard" / "ui.py"

# Community Cloud installs the requirements file, not the project, so nothing
# has put the package on the path.
sys.path.insert(0, str(_REPO / "src"))

# setdefault, not assignment, so a local run of this file can still override.
os.environ.setdefault("PG_DEMO", "1")
os.environ.setdefault("TIER2_DEVICE", "cpu")
# The whole reason this deployment is possible. The fp32 medium checkpoint is
# 1,780 MB resident against a ~1,000 MB ceiling here; the bf16 weights in the
# same commit are 845 MB at unchanged coverage (findings §27.5).
os.environ.setdefault("TIER2_VARIANT", "bf16")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import streamlit as st  # noqa: E402

from pipelineguard import prefetch  # noqa: E402
from pipelineguard.config import settings  # noqa: E402
from pipelineguard.detectors.tier2_encoder import (  # noqa: E402
    cached_main_revision,
)


@st.cache_resource(show_spinner="Fetching the pinned model. First boot only.")
def warm_cache() -> int:
    """Populate the cache, then close the network. Returns the prefetch code.

    cache_resource because Streamlit re-runs this file on every interaction,
    and both halves of this are once-per-container work.
    """
    # prefetch_pinned() writes the ref last, after both downloads, so the ref
    # already pointing at the pin means an earlier boot finished the job.
    if (cached_main_revision(settings.tier2_base_model)
            == settings.tier2_base_revision):
        code = 0
    else:
        code = prefetch.main()

    if code == 0:
        prefetch.go_offline()
    return code


if warm_cache() != 0:
    # Degrade rather than fail. Offline with an incomplete cache cannot load at
    # all, whereas online resolves the backbone at `main` -- unpinned, but
    # working. The provenance panel in the sidebar says which one happened.
    st.warning(
        "The pinned prefetch did not finish, so the backbone config resolves "
        "at `main` and this build is running unpinned. Detection still works."
    )

runpy.run_path(str(_UI), run_name="__main__")