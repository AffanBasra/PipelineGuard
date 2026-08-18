"""Write docs/sample-summary.md from the audit trail.

The demo build has no database behind it, so its governance tab renders this
stored run instead. Real output over synthetic records -- decisions.md section 1
rules out real personal data anywhere in this project, so the file is safe to
commit and a mockup would have been worse.

Needs the stack up:

    docker compose up -d postgres
    venv/Scripts/python.exe scripts/make_sample_summary.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from pipelineguard import report
from pipelineguard.config import settings

OUT = Path(__file__).resolve().parent.parent / "docs" / "sample-summary.md"


def main() -> int:
    import psycopg

    try:
        conn = psycopg.connect(settings.postgres_dsn, connect_timeout=5)
    except Exception as exc:  # noqa: BLE001 - the cause is what matters here
        print(f"could not reach {settings.postgres_dsn}: {exc}", file=sys.stderr)
        print("start it with: docker compose up -d postgres", file=sys.stderr)
        return 1
    try:
        data = report.fetch(conn, None, None, 50)
    finally:
        conn.close()

    if not data.records:
        print("the audit trail is empty; run the producer first", file=sys.stderr)
        return 1

    OUT.write_text(report.render_summary(data), encoding="utf-8")
    print(f"wrote {OUT} from {data.records:,} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
