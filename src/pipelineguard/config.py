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
    tier2_threshold: float = float(os.getenv("TIER2_THRESHOLD", "0.55"))
    tier2_device: str = os.getenv("TIER2_DEVICE", "auto")
    tier2_batch_size: int = int(os.getenv("TIER2_BATCH_SIZE", "8"))


settings = Settings()
