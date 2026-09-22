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
    status         TEXT NOT NULL DEFAULT 'validated',
    request_json   TEXT NOT NULL,
    result_json    TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS void_events (
    invoice_id TEXT PRIMARY KEY NOT NULL REFERENCES invoices(id),
    voided_at  TEXT NOT NULL,
    operator   TEXT NOT NULL,
    reason     TEXT NOT NULL
);

-- Void events are an append-only audit trail: they can be inserted once but
-- never updated or deleted (enforced in the database itself).
CREATE TRIGGER IF NOT EXISTS void_events_no_update
BEFORE UPDATE ON void_events
BEGIN
    SELECT RAISE(ABORT, 'void events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS void_events_no_delete
BEFORE DELETE ON void_events
BEGIN
    SELECT RAISE(ABORT, 'void events are immutable');
END;
"""


class InvoiceNotFoundError(LookupError):
    """Raised when no invoice exists for the given id."""

    def __init__(self, invoice_id: str):
        self.invoice_id = invoice_id
        super().__init__(f"invoice not found: {invoice_id}")


class InvoiceVoidConflictError(Exception):
    """Raised when an invoice cannot be voided in its current status.

    ``code`` is either ``already_voided`` (a void event already exists) or
    ``not_voidable`` (the invoice status is not ``validated``).
    """

    def __init__(self, invoice_id: str, status: str, code: str, message: str):
        self.invoice_id = invoice_id
        self.status = status
        self.code = code
        super().__init__(message)


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
            self._migrate_status_column(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # OFF/ON pragmas must be issued outside a transaction.
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _migrate_status_column(conn: sqlite3.Connection) -> None:
        """Upgrade databases created before invoice voiding existed."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(invoices)")}
        if "status" not in columns:
            conn.execute(
                "ALTER TABLE invoices ADD COLUMN status TEXT NOT NULL"
                " DEFAULT 'validated'"
            )
            conn.execute(
                "UPDATE invoices SET status = json_extract(result_json, '$.status')"
            )

    def insert(self, invoice_id: str, invoice_number: str, request: dict, result: dict, created_at: str) -> None:
        """Insert a computed invoice. Raises sqlite3.IntegrityError on duplicate number."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO invoices (id, invoice_number, status, request_json, result_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    invoice_id,
                    invoice_number,
                    result["status"],
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
                        "INSERT INTO invoices (id, invoice_number, status, request_json, result_json, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            row["invoice_id"],
                            row["invoice_number"],
                            row["result"]["status"],
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
        """Return the stored invoice, with the live status and void event (if any)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT i.result_json, i.status, v.voided_at, v.operator, v.reason"
                " FROM invoices i"
                " LEFT JOIN void_events v ON v.invoice_id = i.id"
                " WHERE i.id = ?",
                (invoice_id,),
            ).fetchone()
        if row is None:
            return None
        result = json.loads(row["result_json"])
        # Status is authoritative from the invoices row; the computed result
        # captured it only at creation time.
        result["status"] = row["status"]
        if row["voided_at"] is not None:
            result["void"] = {
                "voided_at": row["voided_at"],
                "operator": row["operator"],
                "reason": row["reason"],
            }
        else:
            result["void"] = None
        return result

    def list_summaries(
        self, limit: int = 50, offset: int = 0, include_voided: bool = False
    ) -> list[dict]:
        sql = (
            "SELECT i.id, i.invoice_number, i.status, i.result_json, i.created_at"
            " FROM invoices i"
        )
        if not include_voided:
            sql += " WHERE i.status != 'voided'"
        sql += " ORDER BY i.created_at DESC, i.id LIMIT ? OFFSET ?"
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

    def void_invoice(
        self, invoice_id: str, reason: str, operator: str, voided_at: str
    ) -> dict:
        """Void a validated invoice atomically.

        In a single SQLite (``BEGIN IMMEDIATE``) transaction we append the
        immutable void event and flip the invoice status to ``voided``. The
        ``void_events`` primary key guarantees at most one event per invoice,
        so a concurrent or repeated void can never produce a second event.

        Raises InvoiceNotFoundError for an unknown id, or
        InvoiceVoidConflictError when the invoice is already voided (code
        ``already_voided``) or not in ``validated`` status (code
        ``not_voidable``); in every conflict case nothing is written.
        """
        conn = self._connect()
        try:
            # Take the write lock up front so concurrent voids serialize.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
            if row is None:
                raise InvoiceNotFoundError(invoice_id)
            if row["status"] == "voided":
                raise InvoiceVoidConflictError(
                    invoice_id,
                    "voided",
                    "already_voided",
                    f"invoice {invoice_id} has already been voided",
                )
            if row["status"] != "validated":
                raise InvoiceVoidConflictError(
                    invoice_id,
                    row["status"],
                    "not_voidable",
                    f"invoice {invoice_id} cannot be voided: status is "
                    f"'{row['status']}', only 'validated' invoices can be voided",
                )

            # Conditional UPDATE + event PK are defence in depth: under the
            # IMMEDIATE lock only one transaction can reach this point, but a
            # zero-row update means the status changed underneath us.
            cursor = conn.execute(
                "UPDATE invoices SET status = 'voided' WHERE id = ? AND status = 'validated'",
                (invoice_id,),
            )
            if cursor.rowcount != 1:
                raise InvoiceVoidConflictError(
                    invoice_id,
                    row["status"],
                    "not_voidable",
                    f"invoice {invoice_id} could not be marked voided",
                )
            conn.execute(
                "INSERT INTO void_events (invoice_id, voided_at, operator, reason)"
                " VALUES (?, ?, ?, ?)",
                (invoice_id, voided_at, operator, reason),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return self.get_result(invoice_id)
