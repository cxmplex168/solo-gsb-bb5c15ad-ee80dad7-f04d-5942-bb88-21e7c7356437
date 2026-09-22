"""FastAPI application: multi-region invoice allocation API."""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .db import (
    InvoiceNotFoundError,
    InvoiceStore,
    InvoiceVoidConflictError,
)
from .schemas import (
    InvoiceBatchCreate,
    InvoiceBatchItemResult,
    InvoiceBatchResult,
    InvoiceCreate,
    InvoiceResult,
    InvoiceSummary,
    InvoiceVoidRequest,
)
from .service import TotalsMismatchError, compute_invoice

DEFAULT_DB_PATH = os.environ.get("DATABASE_PATH", "data/invoices.db")

_store: InvoiceStore | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize (and, for existing volumes, migrate) the SQLite schema at
    # container startup instead of waiting for the first request.
    get_store()
    yield


app = FastAPI(
    title="Multi-Region Invoice Allocation API",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "Computes exact (Decimal) invoice discounts and per-tax-region amounts, "
        "distributes rounding residuals with the largest remainder method, and "
        "rejects inconsistent proportions or totals."
    ),
)


def get_store() -> InvoiceStore:
    global _store
    if _store is None:
        _store = InvoiceStore(DEFAULT_DB_PATH)
    return _store


def to_jsonable(obj: Any) -> Any:
    """Convert Decimals to JSON numbers; amounts are already cent-quantized."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_jsonable(v) for v in obj]
    return obj


def result_response(result: InvoiceResult, status_code: int = 200) -> JSONResponse:
    # exclude_none keeps the "void" key absent for non-voided invoices.
    return JSONResponse(
        status_code=status_code,
        content=to_jsonable(result.model_dump(exclude_none=True)),
    )


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "service": "multi-region-invoice-allocation",
        "version": "1.0.0",
        "docs": "/docs",
        "endpoints": {
            "create_invoice": "POST /api/v1/invoices",
            "create_invoices_batch": "POST /api/v1/invoices/batch",
            "get_invoice": "GET /api/v1/invoices/{invoice_id}",
            "void_invoice": "POST /api/v1/invoices/{invoice_id}/void",
            "list_invoices": "GET /api/v1/invoices",
        },
    }


@app.post(
    "/api/v1/invoices",
    status_code=201,
    response_model=InvoiceResult,
    tags=["invoices"],
    responses={
        400: {"description": "Expected totals do not match computed totals"},
        409: {"description": "Invoice number already exists"},
        422: {"description": "Invalid input (e.g. proportions do not sum to 1)"},
    },
)
def create_invoice(
    payload: InvoiceCreate, store: InvoiceStore = Depends(get_store)
) -> JSONResponse:
    try:
        result = compute_invoice(payload)
    except TotalsMismatchError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "totals_mismatch",
                "message": "expected totals do not match computed totals",
                "mismatches": exc.mismatches,
            },
        )

    try:
        store.insert(
            invoice_id=result.invoice_id,
            invoice_number=result.invoice_number,
            request=payload.model_dump(),
            result=result.model_dump(),
            created_at=result.created_at,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"invoice_number '{payload.invoice_number}' already exists",
        )

    return result_response(result, status_code=201)


@app.post(
    "/api/v1/invoices/batch",
    status_code=201,
    response_model=InvoiceBatchResult,
    tags=["invoices"],
    responses={
        400: {"description": "One or more invoices failed validation (e.g. expected totals mismatch); nothing was written"},
        409: {"description": "Duplicate invoice number (within the batch or already stored); nothing was written"},
        422: {"description": "Invalid input (e.g. proportions do not sum to 1)"},
    },
)
def create_invoices_batch(
    payload: InvoiceBatchCreate, store: InvoiceStore = Depends(get_store)
) -> JSONResponse:
    """Create several invoices atomically.

    All invoices are computed and validated first; only if every one of them
    passes are they written, in a single transaction. Any duplicate invoice
    number or expected-totals mismatch rejects the whole batch, and the error
    response identifies the offending invoices by their request index.
    """
    # --- Phase 1: compute and validate every invoice (nothing written yet) --
    computed: list[InvoiceResult] = []
    calc_errors: list[dict] = []
    for idx, invoice in enumerate(payload.invoices):
        try:
            computed.append(compute_invoice(invoice))
        except TotalsMismatchError as exc:
            calc_errors.append(
                {
                    "index": idx,
                    "invoice_number": invoice.invoice_number,
                    "error": "totals_mismatch",
                    "mismatches": exc.mismatches,
                }
            )
    if calc_errors:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "batch_rejected",
                "message": "one or more invoices failed validation; nothing was written",
                "errors": calc_errors,
            },
        )

    # --- Phase 2: duplicate invoice numbers (within batch or already stored)
    dup_errors: list[dict] = []
    seen: dict[str, int] = {}
    for idx, invoice in enumerate(payload.invoices):
        first = seen.setdefault(invoice.invoice_number, idx)
        if first != idx:
            dup_errors.append(
                {
                    "index": idx,
                    "invoice_number": invoice.invoice_number,
                    "error": "duplicate_invoice_number",
                    "message": f"duplicates invoice at index {first} in this batch",
                }
            )
    existing = store.existing_numbers([inv.invoice_number for inv in payload.invoices])
    reported = {e["index"] for e in dup_errors}
    for idx, invoice in enumerate(payload.invoices):
        if invoice.invoice_number in existing and idx not in reported:
            dup_errors.append(
                {
                    "index": idx,
                    "invoice_number": invoice.invoice_number,
                    "error": "duplicate_invoice_number",
                    "message": "invoice number already exists",
                }
            )
    if dup_errors:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "batch_rejected",
                "message": "duplicate invoice numbers; nothing was written",
                "errors": dup_errors,
            },
        )

    # --- Phase 3: write all invoices in a single transaction ----------------
    try:
        store.insert_many(
            [
                {
                    "invoice_id": result.invoice_id,
                    "invoice_number": result.invoice_number,
                    "request": invoice.model_dump(),
                    "result": result.model_dump(),
                    "created_at": result.created_at,
                }
                for invoice, result in zip(payload.invoices, computed)
            ]
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "batch_rejected",
                "message": f"duplicate invoice number; nothing was written ({exc})",
            },
        )

    result = InvoiceBatchResult(
        invoices=[
            InvoiceBatchItemResult(
                index=idx,
                invoice_id=r.invoice_id,
                invoice_number=r.invoice_number,
                grand_total=r.totals.grand_total,
            )
            for idx, r in enumerate(computed)
        ]
    )
    return JSONResponse(
        status_code=201, content=to_jsonable(result.model_dump())
    )


@app.get(
    "/api/v1/invoices/{invoice_id}",
    response_model=InvoiceResult,
    tags=["invoices"],
    responses={404: {"description": "Invoice not found"}},
)
def get_invoice(
    invoice_id: str, store: InvoiceStore = Depends(get_store)
) -> JSONResponse:
    stored = store.get_result(invoice_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    # Re-validate so Decimal fields round-trip exactly, then render as numbers.
    result = InvoiceResult.model_validate(stored)
    return result_response(result)


@app.post(
    "/api/v1/invoices/{invoice_id}/void",
    response_model=InvoiceResult,
    tags=["invoices"],
    responses={
        404: {"description": "Invoice not found"},
        409: {"description": "Invoice already voided, or not in validated status"},
        422: {"description": "Invalid input (missing or blank reason/operator)"},
    },
)
def void_invoice(
    invoice_id: str,
    payload: InvoiceVoidRequest,
    store: InvoiceStore = Depends(get_store),
) -> JSONResponse:
    """Void (red-flush) a validated invoice.

    Only invoices with status ``validated`` can be voided. The immutable void
    event (time, operator, reason) is appended and the invoice status is
    updated to ``voided`` in a single SQLite transaction. Repeating the void
    returns 409 without ever writing a second event.
    """
    voided_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        stored = store.void_invoice(
            invoice_id=invoice_id,
            reason=payload.reason,
            operator=payload.operator,
            voided_at=voided_at,
        )
    except InvoiceNotFoundError:
        raise HTTPException(status_code=404, detail="invoice not found")
    except InvoiceVoidConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": exc.code,
                "message": str(exc),
                "invoice_id": invoice_id,
                "status": exc.status,
            },
        )

    result = InvoiceResult.model_validate(stored)
    return result_response(result)


@app.get("/api/v1/invoices", response_model=list[InvoiceSummary], tags=["invoices"])
def list_invoices(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    include_voided: bool = Query(
        default=False,
        description="Set true to include voided invoices; they are excluded by default",
    ),
    store: InvoiceStore = Depends(get_store),
) -> JSONResponse:
    return JSONResponse(
        content=to_jsonable(
            store.list_summaries(limit, offset, include_voided=include_voided)
        )
    )
