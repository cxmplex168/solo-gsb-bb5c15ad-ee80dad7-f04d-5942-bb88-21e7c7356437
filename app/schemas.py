"""Request and response schemas for the invoice allocation API."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Optional

from pydantic import BaseModel, Field, StringConstraints, model_validator

from .allocation import PROPORTION_TOLERANCE


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class TaxRegionShare(BaseModel):
    """One tax region's share of a line's net amount."""

    region_code: str = Field(min_length=1, max_length=32, examples=["CN-SH"])
    tax_rate: Decimal = Field(ge=0, le=1, description="Tax rate as a fraction, e.g. 0.13 for 13%")
    proportion: Decimal = Field(gt=0, le=1, description="Share of the line net amount; all regions of a line must sum to 1")


class InvoiceLineIn(BaseModel):
    description: str = Field(min_length=1, max_length=200)
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    discount_rate: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    discount_amount: Decimal = Field(default=Decimal("0"), ge=0)
    tax_regions: list[TaxRegionShare] = Field(min_length=1)

    @model_validator(mode="after")
    def check_consistency(self) -> "InvoiceLineIn":
        codes = [r.region_code for r in self.tax_regions]
        if len(set(codes)) != len(codes):
            raise ValueError(f"duplicate tax region codes in line: {sorted(set(codes))}")

        proportion_sum = sum((r.proportion for r in self.tax_regions), Decimal("0"))
        if abs(proportion_sum - Decimal("1")) > PROPORTION_TOLERANCE:
            raise ValueError(
                f"tax region proportions must sum to 1, got {proportion_sum}"
            )

        gross = self.quantity * self.unit_price
        discount = gross * self.discount_rate + self.discount_amount
        if discount > gross:
            raise ValueError(
                f"discount ({discount}) exceeds gross amount ({gross})"
            )
        return self


class ExpectedTotals(BaseModel):
    """Optional client-side totals; the invoice is rejected on any mismatch."""

    net: Optional[Decimal] = None
    tax: Optional[Decimal] = None
    grand_total: Optional[Decimal] = None


class InvoiceCreate(BaseModel):
    invoice_number: str = Field(min_length=1, max_length=64)
    currency: str = Field(default="CNY", pattern="^[A-Z]{3}$")
    lines: list[InvoiceLineIn] = Field(min_length=1)
    expected_totals: Optional[ExpectedTotals] = None


class InvoiceBatchCreate(BaseModel):
    """Atomic batch: every invoice is computed and validated first, then all
    are written in a single transaction; any failure rolls the batch back."""

    invoices: list[InvoiceCreate] = Field(min_length=1, max_length=100)


class InvoiceVoidRequest(BaseModel):
    """Void a validated invoice. Both fields are required and recorded in the
    immutable void event."""

    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] = Field(
        description="Why the invoice is being voided"
    )
    operator: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] = Field(
        description="Who performs the void (recorded as voided_by)"
    )


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class RegionBreakdown(BaseModel):
    region_code: str
    tax_rate: Decimal
    proportion: Decimal
    taxable_amount: Decimal
    tax_amount: Decimal


class LineResult(BaseModel):
    line_number: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    gross_amount: Decimal
    discount_rate: Decimal
    discount_amount: Decimal
    net_amount: Decimal
    regions: list[RegionBreakdown]
    tax_amount: Decimal
    line_total: Decimal


class RegionTotal(BaseModel):
    region_code: str
    taxable_amount: Decimal
    tax_amount: Decimal


class Totals(BaseModel):
    total_gross: Decimal
    total_discount: Decimal
    total_net: Decimal
    total_tax: Decimal
    grand_total: Decimal
    region_totals: list[RegionTotal]


class CheckItem(BaseModel):
    name: str
    ok: bool
    detail: str


class ValidationReport(BaseModel):
    all_ok: bool
    checks: list[CheckItem]


class InvoiceResult(BaseModel):
    invoice_id: str
    invoice_number: str
    currency: str
    status: str
    lines: list[LineResult]
    totals: Totals
    validation: ValidationReport
    created_at: str
    # Populated only after the invoice has been voided.
    voided_at: Optional[str] = None
    voided_by: Optional[str] = None
    void_reason: Optional[str] = None


class InvoiceSummary(BaseModel):
    invoice_id: str
    invoice_number: str
    currency: str
    status: str
    grand_total: Decimal
    created_at: str


class InvoiceBatchItemResult(BaseModel):
    """Per-invoice summary returned by the batch endpoint."""

    index: int
    invoice_id: str
    invoice_number: str
    grand_total: Decimal


class InvoiceBatchResult(BaseModel):
    invoices: list[InvoiceBatchItemResult]
