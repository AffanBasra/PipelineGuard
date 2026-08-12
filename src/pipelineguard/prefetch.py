"""Warm the model cache with exactly the pinned commits, then run offline.

Tier 2 touches two HuggingFace repos, and `TIER2_MODEL_REVISION` only pins one
of them. GLiNER reads its backbone architecture with
`AutoConfig.from_pretrained(config.model_name)` and passes no revision, so
`microsoft/deberta-v3-base` is resolved at `main` however tightly the checkpoint
is pinned (findings §25).

GLiNER exposes no hook to change that, so the backbone is pinned the only way
left: put exactly the pinned commit in the cache, then forbid the network.
Offline resolution can only return what is already there.

    python -m pipelineguard.prefetch      # once, with network
    HF_HUB_OFFLINE=1 ...                  # from then on

In Docker the cache is the `models` volume, so this is a one-off per volume:

    docker compose run --rm processor python -m pipelineguard.prefetch
    HF_HUB_OFFLINE=1 docker compose up -d processor
"""
from __future__ import annotations

import logging
import os
import sys

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from pipelineguard.config import settings  # noqa: E402
from pipelineguard.observability import setup_logging  # noqa: E402

log = logging.getLogger("pipelineguard.prefetch")


def main(argv: list[str] | None = None) -> int:
    setup_logging("INFO")

    if os.getenv("HF_HUB_OFFLINE", "").strip().lower() in {"1", "true"}:
        log.error("HF_HUB_OFFLINE is set; this command needs the network. "
                  "Unset it, run the prefetch, then set it again.")
        return 2

    try:
        from pipelineguard.detectors.tier2_encoder import (
            cached_base_revisions, prefetch_pinned,
        )
    except ImportError:
        log.error("needs the tier2 extra: pip install '.[tier2]'")
        return 2

    log.info("cache: %s", os.getenv("HF_HOME") or "(default)")
    prefetch_pinned(settings.tier2_model, settings.tier2_model_revision,
                    settings.tier2_base_model, settings.tier2_base_revision)

    cached = cached_base_revisions(settings.tier2_base_model)
    if cached == [settings.tier2_base_revision]:
        log.info("backbone cache holds exactly the pinned commit -- "
                 "set HF_HUB_OFFLINE=1 and the pin is enforced")
        return 0

    # Not fatal: an extra commit is usually an older cache, and offline mode
    # will still resolve. But it means the cache no longer proves WHICH one a
    # load picks, so the operator has to be told.
    log.warning("backbone cache holds %s, expected exactly [%s]. Offline mode "
                "will resolve one of them, and which is not guaranteed. Delete "
                "the repo from the cache and re-run to make the pin exact.",
                [r[:12] for r in cached], settings.tier2_base_revision[:12])
    return 1


if __name__ == "__main__":
    sys.exit(main())
