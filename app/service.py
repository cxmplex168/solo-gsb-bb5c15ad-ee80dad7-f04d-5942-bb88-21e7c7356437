"""Invoice computation: discounts, per-region tax allocation, validation."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from .allocation import PROPORTION_TOLERANCE, largest_remainder, q2
from .schemas import (
    CheckItem,
    InvoiceCreate,
    InvoiceResult,
    LineResult,
    RegionBreakdown,
    RegionTotal,
    Totals,
    ValidationReport,
)

ZERO = Decimal("0")


class TotalsMismatchError(Exception):
    """Raised when client-supplied expected totals do not match the computed ones."""

    def __init__(self, mismatches: list[str]):
        self.mismatches = mismatches
        super().__init__("; ".join(mismatches))


def compute_invoice(payload: InvoiceCreate) -> InvoiceResult:
    """Compute a fully allocated, self-consistent invoice result.

    Steps per line: gross -> discount -> net -> split net across tax regions
    (largest remainder on the exact proportional shares) -> exact tax per
    region. Then, for each region, the invoice-level tax total is rounded
    once and distributed back to the lines with the largest remainder method,
    so every rounded figure adds up exactly.
    """
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # --- Pass 1: per-line amounts and exact (unrounded) region taxes --------
    line_drafts: list[dict] = []
    region_codes: list[str] = []
    for pos, line in enumerate(payload.lines, start=1):
        gross = q2(line.quantity * line.unit_price)
        discount = q2(gross * line.discount_rate + line.discount_amount)
        net = gross - discount

        # Split the line net across its regions by proportion.
        taxable_amounts = largest_remainder(
            net, [net * r.proportion for r in line.tax_regions]
        )

        regions = []
        for region, taxable in zip(line.tax_regions, taxable_amounts):
            if region.region_code not in region_codes:
                region_codes.append(region.region_code)
            regions.append(
                {
                    "region": region,
                    "taxable": taxable,
                    "tax_exact": taxable * region.tax_rate,
                }
            )

        line_drafts.append(
            {
                "pos": pos,
                "line": line,
                "gross": gross,
                "discount": discount,
                "net": net,
                "regions": regions,
            }
        )

    # --- Pass 2: per-region invoice totals, allocated back to lines ---------
    # For each region, round the exact invoice-level tax once, then use the
    # largest remainder method to distribute it across lines so the rounded
    # per-line amounts sum exactly to the region total.
    allocated_tax: list[dict[str, Decimal]] = [dict() for _ in line_drafts]
    region_totals: list[RegionTotal] = []
    for code in region_codes:
        exact_shares = []
        taxable_sum = ZERO
        for draft in line_drafts:
            entry = next(
                (e for e in draft["regions"] if e["region"].region_code == code),
                None,
            )
            exact_shares.append(entry["tax_exact"] if entry else ZERO)
            taxable_sum += entry["taxable"] if entry else ZERO

        region_tax_total = q2(sum(exact_shares))
        per_line_amounts = largest_remainder(region_tax_total, exact_shares)
        for idx, amount in enumerate(per_line_amounts):
            allocated_tax[idx][code] = amount
        region_totals.append(
            RegionTotal(
                region_code=code,
                taxable_amount=taxable_sum,
                tax_amount=region_tax_total,
            )
        )

    # --- Pass 3: assemble line results and invoice totals -------------------
    lines_out: list[LineResult] = []
    for idx, draft in enumerate(line_drafts):
        line = draft["line"]
        regions_out = [
            RegionBreakdown(
                region_code=entry["region"].region_code,
                tax_rate=entry["region"].tax_rate,
                proportion=entry["region"].proportion,
                taxable_amount=entry["taxable"],
                tax_amount=allocated_tax[idx][entry["region"].region_code],
            )
            for entry in draft["regions"]
        ]
        line_tax = sum((r.tax_amount for r in regions_out), ZERO)
        lines_out.append(
            LineResult(
                line_number=draft["pos"],
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                gross_amount=draft["gross"],
                discount_rate=line.discount_rate,
                discount_amount=draft["discount"],
                net_amount=draft["net"],
                regions=regions_out,
                tax_amount=line_tax,
                line_total=draft["net"] + line_tax,
            )
        )

    total_gross = sum((l.gross_amount for l in lines_out), ZERO)
    total_discount = sum((l.discount_amount for l in lines_out), ZERO)
    total_net = sum((l.net_amount for l in lines_out), ZERO)
    total_tax = sum((r.tax_amount for r in region_totals), ZERO)
    grand_total = total_net + total_tax

    totals = Totals(
        total_gross=total_gross,
        total_discount=total_discount,
        total_net=total_net,
        total_tax=total_tax,
        grand_total=grand_total,
        region_totals=region_totals,
    )

    # --- Validation report ---------------------------------------------------
    checks: list[CheckItem] = []

    proportion_ok = all(
        abs(sum((r.proportion for r in line.tax_regions), ZERO) - Decimal("1"))
        <= PROPORTION_TOLERANCE
        for line in payload.lines
    )
    checks.append(
        CheckItem(
            name="line_proportions_sum_to_one",
            ok=proportion_ok,
            detail="every line's tax region proportions sum to 1",
        )
    )

    net_ok = all(
        l.net_amount == l.gross_amount - l.discount_amount for l in lines_out
    ) and total_net == total_gross - total_discount
    checks.append(
        CheckItem(
            name="net_equals_gross_minus_discount",
            ok=net_ok,
            detail=f"total_net {total_net} = total_gross {total_gross} - total_discount {total_discount}",
        )
    )

    taxable_ok = all(
        sum((r.taxable_amount for r in l.regions), ZERO) == l.net_amount
        for l in lines_out
    )
    checks.append(
        CheckItem(
            name="region_taxable_sums_equal_line_net",
            ok=taxable_ok,
            detail="per line, region taxable amounts add up to the line net amount",
        )
    )

    allocation_ok = all(
        sum(
            (allocated_tax[i].get(code, ZERO) for i in range(len(line_drafts))),
            ZERO,
        )
        == rt.tax_amount
        for code, rt in zip(region_codes, region_totals)
    )
    checks.append(
        CheckItem(
            name="allocated_region_taxes_sum_to_region_totals",
            ok=allocation_ok,
            detail="per region, allocated line taxes add up to the region tax total",
        )
    )

    line_tax_ok = all(
        l.tax_amount == sum((r.tax_amount for r in l.regions), ZERO)
        and l.line_total == l.net_amount + l.tax_amount
        for l in lines_out
    ) and sum((l.line_total for l in lines_out), ZERO) == grand_total
    checks.append(
        CheckItem(
            name="grand_total_equals_net_plus_tax",
            ok=line_tax_ok and grand_total == total_net + total_tax,
            detail=f"grand_total {grand_total} = total_net {total_net} + total_tax {total_tax}",
        )
    )

    validation = ValidationReport(all_ok=all(c.ok for c in checks), checks=checks)

    # --- Reject if client-supplied expected totals disagree ------------------
    if payload.expected_totals is not None:
        mismatches = []
        expected = payload.expected_totals
        for label, given, computed in (
            ("net", expected.net, total_net),
            ("tax", expected.tax, total_tax),
            ("grand_total", expected.grand_total, grand_total),
        ):
            if given is not None and given != computed:
                mismatches.append(f"{label}: expected {given}, computed {computed}")
        if mismatches:
            raise TotalsMismatchError(mismatches)

    return InvoiceResult(
        invoice_id=uuid4().hex,
        invoice_number=payload.invoice_number,
        currency=payload.currency,
        status="validated" if validation.all_ok else "inconsistent",
        lines=lines_out,
        totals=totals,
        validation=validation,
        created_at=now,
    )
