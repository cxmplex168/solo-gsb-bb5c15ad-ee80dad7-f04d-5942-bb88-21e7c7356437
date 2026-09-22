"""SQLite persistence for computed invoices (stdlib sqlite3, no ORM)."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id             TEXT PRIMARY KEY,
    invoice_number TEXT NOT NULL UNIQUE,
    request_json   TEXT NOT NULL,
    result_json    TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
"""


class DecimalEncoder(json.JSONEncoder):
    """Store Decimals losslessly as strings."""

    def default(self, o):
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


class InvoiceStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def insert(self, invoice_id: str, invoice_number: str, request: dict, result: dict, created_at: str) -> None:
        """Insert a computed invoice. Raises sqlite3.IntegrityError on duplicate number."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO invoices (id, invoice_number, request_json, result_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    invoice_id,
                    invoice_number,
                    json.dumps(request, cls=DecimalEncoder),
                    json.dumps(result, cls=DecimalEncoder),
                    created_at,
                ),
            )

    def insert_many(self, rows: list[dict]) -> None:
        """Insert several invoices in one transaction.

        Every row is a dict with keys invoice_id, invoice_number, request,
        result, created_at. If any insert fails (e.g. sqlite3.IntegrityError
        on a duplicate invoice_number), the whole transaction is rolled back
        and nothing is persisted.
        """
        conn = self._connect()
        try:
            with conn:  # commits on success, rolls back on any exception
                for row in rows:
                    conn.execute(
                        "INSERT INTO invoices (id, invoice_number, request_json, result_json, created_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (
                            row["invoice_id"],
                            row["invoice_number"],
                            json.dumps(row["request"], cls=DecimalEncoder),
                            json.dumps(row["result"], cls=DecimalEncoder),
                            row["created_at"],
                        ),
                    )
        finally:
            conn.close()

    def existing_numbers(self, invoice_numbers: list[str]) -> set[str]:
        """Return the subset of ``invoice_numbers`` already stored."""
        if not invoice_numbers:
            return set()
        placeholders = ",".join("?" for _ in invoice_numbers)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT invoice_number FROM invoices WHERE invoice_number IN ({placeholders})",
                invoice_numbers,
            ).fetchall()
        return {row["invoice_number"] for row in rows}

    def get_result(self, invoice_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
        return json.loads(row["result_json"]) if row else None

    def list_summaries(self, limit: int = 50, offset: int = 0) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, invoice_number, result_json, created_at"
                " FROM invoices ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        summaries = []
        for row in rows:
            result = json.loads(row["result_json"])
            summaries.append(
                {
                    "invoice_id": row["id"],
                    "invoice_number": row["invoice_number"],
                    "currency": result["currency"],
                    "grand_total": Decimal(result["totals"]["grand_total"]),
                    "created_at": row["created_at"],
                }
            )
        return summaries
