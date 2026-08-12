"""Central config, loaded from .env (falls back to compose defaults)."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    kafka_bootstrap: str = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
    # Port 5433, matching docker-compose.yml. A native Postgres install commonly
    # occupies 5432, so defaulting there would silently connect to the wrong
    # database rather than failing loudly.
    postgres_dsn: str = os.getenv(
        "POSTGRES_DSN",
        "postgresql://pipelineguard:pipelineguard@localhost:5433/pipelineguard",
    )
    topic_txn_raw: str = os.getenv("TOPIC_TXN_RAW", "txn.raw")
    topic_txn_clean: str = os.getenv("TOPIC_TXN_CLEAN", "txn.clean")
    topic_txn_quarantine: str = os.getenv("TOPIC_TXN_QUARANTINE", "txn.quarantine")
    consumer_group: str = os.getenv("CONSUMER_GROUP", "pipelineguard-processor")

    # Tier 2. The model and threshold are a PAIR -- 0.55 is where this
    # checkpoint operates, and it does not transfer (findings §6.1, §16.2).
    # At urchade's 0.25 this model fires on 76% of clean Pakistani memos; at
    # 0.55 it fires on 20%, against urchade's 40% at its own best point.
    # Changing one without re-sweeping the other produces worse results than
    # either default. Device falls back to CPU when CUDA is unavailable, and
    # batch 8 saturates throughput.
    tier2_enabled: bool = os.getenv("TIER2_ENABLED", "false").lower() == "true"
    tier2_model: str = os.getenv(
        "TIER2_MODEL", "gliner-community/gliner_medium-v2.5"
    )
    # The exact commit every number in tier2-detection-findings.md §14-§19 was
    # measured against. Without it "we chose this checkpoint" is an intention,
    # not a fact: HuggingFace can republish weights under the same name, and
    # §6.1's rule is that a threshold does not survive a checkpoint change. The
    # name would not change, so nothing would warn us. Set to "main" to track
    # the branch instead, and re-sweep the threshold if you do.
    tier2_model_revision: str = os.getenv(
        "TIER2_MODEL_REVISION", "88c3b98b57ad5e7d66fb209ed61c53f4b1fd05da"
    )
    # The SECOND repo a Tier 2 load touches, and the half the revision above
    # does not cover (§25). GLiNER calls AutoConfig.from_pretrained() on its
    # backbone name with no revision, so `microsoft/deberta-v3-base` is fetched
    # at `main` no matter how tightly the weights are pinned. GLiNER exposes no
    # way to pass a revision through, so this is pinned by PREFETCHING exactly
    # this commit into the cache and then running with HF_HUB_OFFLINE=1 --
    # scripts/verify_tier2_pin.py checks both halves.
    tier2_base_model: str = os.getenv("TIER2_BASE_MODEL",
                                      "microsoft/deberta-v3-base")
    tier2_base_revision: str = os.getenv(
        "TIER2_BASE_REVISION", "8ccc9b6f36199bec6961081d44eb72fb3f7353f3"
    )
    tier2_threshold: float = float(os.getenv("TIER2_THRESHOLD", "0.55"))
    tier2_device: str = os.getenv("TIER2_DEVICE", "auto")
    tier2_batch_size: int = int(os.getenv("TIER2_BATCH_SIZE", "8"))


settings = Settings()
