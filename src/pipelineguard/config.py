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


settings = Settings()
