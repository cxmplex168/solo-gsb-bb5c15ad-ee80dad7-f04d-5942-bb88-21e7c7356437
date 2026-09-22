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
    created_at     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'validated'
);

-- Immutable audit log: one row per voided invoice, written together with the
-- invoice status flip in a single transaction. Rows are never updated or
-- deleted; the PRIMARY KEY guarantees at most one void event per invoice.
CREATE TABLE IF NOT EXISTS void_events (
    invoice_id TEXT PRIMARY KEY REFERENCES invoices(id),
    reason     TEXT NOT NULL,
    operator   TEXT NOT NULL,
    voided_at  TEXT NOT NULL
);
"""


class InvoiceNotFoundError(Exception):
    """Raised when no invoice exists for the given id."""


class InvoiceAlreadyVoidedError(Exception):
    """Raised when voiding an invoice that is already voided.

    Carries the originally recorded void event so the API can report when and
    by whom the invoice was voided.
    """

    def __init__(self, invoice_id: str, event: Optional[dict] = None):
        self.invoice_id = invoice_id
        self.event = event
        super().__init__(f"invoice '{invoice_id}' is already voided")


class InvoiceNotVoidableError(Exception):
    """Raised when voiding an invoice whose status is not 'validated'."""

    def __init__(self, invoice_id: str, status: str):
        self.invoice_id = invoice_id
        self.status = status
        super().__init__(
            f"invoice '{invoice_id}' has status '{status}', only 'validated' invoices can be voided"
        )


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
        conn = self._connect()
        try:
            with conn:
                conn.executescript(_SCHEMA)
                self._migrate(conn)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Bring databases created before void support up to date.

        Older invoice tables lack the ``status`` column; add it and backfill
        from the stored result JSON.
        """
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(invoices)")}
        if "status" in cols:
            return
        conn.execute(
            "ALTER TABLE invoices ADD COLUMN status TEXT NOT NULL DEFAULT ''"
        )
        rows = conn.execute("SELECT id, result_json FROM invoices").fetchall()
        for row in rows:
            status = json.loads(row["result_json"]).get("status") or "validated"
            conn.execute(
                "UPDATE invoices SET status = ? WHERE id = ?", (status, row["id"])
            )

    def insert(self, invoice_id: str, invoice_number: str, request: dict, result: dict, created_at: str) -> None:
        """Insert a computed invoice. Raises sqlite3.IntegrityError on duplicate number."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO invoices (id, invoice_number, request_json, result_json, created_at, status)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    invoice_id,
                    invoice_number,
                    json.dumps(request, cls=DecimalEncoder),
                    json.dumps(result, cls=DecimalEncoder),
                    created_at,
                    result.get("status", "validated"),
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
                        "INSERT INTO invoices (id, invoice_number, request_json, result_json, created_at, status)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            row["invoice_id"],
                            row["invoice_number"],
                            json.dumps(row["request"], cls=DecimalEncoder),
                            json.dumps(row["result"], cls=DecimalEncoder),
                            row["created_at"],
                            row["result"].get("status", "validated"),
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

    def void_invoice(
        self, invoice_id: str, reason: str, operator: str, voided_at: str
    ) -> dict:
        """Void a validated invoice.

        The immutable void event and the invoice status flip are written in a
        single transaction: either both are persisted or neither is. A repeat
        void (or a void of a non-validated invoice) raises before any write,
        so no second event can ever be recorded.

        Raises InvoiceNotFoundError, InvoiceAlreadyVoidedError or
        InvoiceNotVoidableError. Returns the recorded void event.
        """
        conn = self._connect()
        try:
            with conn:  # commits on success, rolls back on any exception
                row = conn.execute(
                    "SELECT status FROM invoices WHERE id = ?", (invoice_id,)
                ).fetchone()
                if row is None:
                    raise InvoiceNotFoundError(invoice_id)
                if row["status"] == "voided":
                    event = conn.execute(
                        "SELECT reason, operator, voided_at FROM void_events"
                        " WHERE invoice_id = ?",
                        (invoice_id,),
                    ).fetchone()
                    raise InvoiceAlreadyVoidedError(
                        invoice_id, dict(event) if event else None
                    )
                if row["status"] != "validated":
                    raise InvoiceNotVoidableError(invoice_id, row["status"])
                conn.execute(
                    "INSERT INTO void_events (invoice_id, reason, operator, voided_at)"
                    " VALUES (?, ?, ?, ?)",
                    (invoice_id, reason, operator, voided_at),
                )
                conn.execute(
                    "UPDATE invoices SET status = 'voided' WHERE id = ?",
                    (invoice_id,),
                )
        finally:
            conn.close()
        return {
            "invoice_id": invoice_id,
            "reason": reason,
            "operator": operator,
            "voided_at": voided_at,
        }

    def get_invoice(self, invoice_id: str) -> Optional[dict]:
        """Return the stored result merged with the current status and, for
        voided invoices, the void event (voided_at / voided_by / void_reason)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT i.result_json, i.status, v.reason, v.operator, v.voided_at"
                " FROM invoices i"
                " LEFT JOIN void_events v ON v.invoice_id = i.id"
                " WHERE i.id = ?",
                (invoice_id,),
            ).fetchone()
        if row is None:
            return None
        result = json.loads(row["result_json"])
        result["status"] = row["status"]
        result["voided_at"] = row["voided_at"]
        result["voided_by"] = row["operator"]
        result["void_reason"] = row["reason"]
        return result

    def list_summaries(
        self, limit: int = 50, offset: int = 0, include_voided: bool = False
    ) -> list[dict]:
        sql = (
            "SELECT id, invoice_number, result_json, created_at, status"
            " FROM invoices"
        )
        if not include_voided:
            sql += " WHERE status != 'voided'"
        sql += " ORDER BY created_at DESC, id LIMIT ? OFFSET ?"
        with self._connect() as conn:
            rows = conn.execute(sql, (limit, offset)).fetchall()
        summaries = []
        for row in rows:
            result = json.loads(row["result_json"])
            summaries.append(
                {
                    "invoice_id": row["id"],
                    "invoice_number": row["invoice_number"],
                    "currency": result["currency"],
                    "status": row["status"],
                    "grand_total": Decimal(result["totals"]["grand_total"]),
                    "created_at": row["created_at"],
                }
            )
        return summaries
