"""Exact money math for invoice allocation.

All monetary values are handled as ``decimal.Decimal`` and quantized to cents
(0.01) with ROUND_HALF_UP. Rounding residuals are distributed with the
largest remainder method, which is deterministic: ties on the fractional
remainder are broken by the original bucket order, so the same input always
produces the same allocation.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
PROPORTION_TOLERANCE = Decimal("0.000001")


def q2(value: Decimal) -> Decimal:
    """Quantize a monetary value to cents using ROUND_HALF_UP."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def largest_remainder(
    total: Decimal,
    shares: list[Decimal],
    quantum: Decimal = CENT,
) -> list[Decimal]:
    """Split ``total`` across buckets proportionally to their exact ``shares``.

    ``total`` must already be a multiple of ``quantum``. Each bucket first
    receives its share floored to the quantum; the remaining units are then
    handed out one at a time to the buckets with the largest fractional
    remainders (ties broken by bucket order, keeping the result stable).

    The returned amounts are quantized and sum exactly to ``total``.
    """
    if not shares:
        if total != 0:
            raise ValueError("cannot allocate a non-zero total to zero buckets")
        return []

    floors: list[int] = []
    remainders: list[Decimal] = []
    for share in shares:
        units = share / quantum
        floor_units = int(units.to_integral_value(rounding=ROUND_FLOOR))
        floors.append(floor_units)
        remainders.append(units - floor_units)

    total_units = int((total / quantum).to_integral_value(rounding=ROUND_HALF_UP))
    diff = total_units - sum(floors)
    if diff < 0 or diff > len(shares):
        raise ValueError(
            f"shares are inconsistent with total: total={total}, "
            f"sum of floored shares={sum(floors) * quantum}"
        )

    # Deterministic order: largest remainder first, then lowest index.
    order = sorted(range(len(shares)), key=lambda i: (-remainders[i], i))
    for i in order[:diff]:
        floors[i] += 1

    return [q2(units * quantum) for units in floors]
