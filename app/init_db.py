"""Container startup helper: create or migrate the SQLite schema, then exit.

Run as ``python -m app.init_db`` (see docker-entrypoint.sh) so the database —
including the void_events table and the invoices.status column — exists before
the API server starts accepting traffic. Safe to run repeatedly.
"""

from __future__ import annotations

import os

from .db import InvoiceStore

DEFAULT_DB_PATH = os.environ.get("DATABASE_PATH", "data/invoices.db")


def main() -> None:
    InvoiceStore(DEFAULT_DB_PATH)
    print(f"invoice database initialized at {DEFAULT_DB_PATH}")


if __name__ == "__main__":
    main()
