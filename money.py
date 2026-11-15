"""Ex-VAT money for Pretium. VAT is 20%, rounded half-up to the penny."""

from decimal import Decimal, ROUND_HALF_UP

VAT_RATE = Decimal("0.20")
VAT_FACTOR = Decimal("1.20")
FALLBACK_EX_VAT_RATE = Decimal("0.50")
_PENNY = Decimal("0.01")


def _dec(value) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def to_penny(value: Decimal) -> Decimal:
    """Round half-up to the penny."""
    return value.quantize(_PENNY, rounding=ROUND_HALF_UP)


def rate_or_fallback(value) -> Decimal:
    """Use a stored ex-VAT rate. £0 stays £0. Only a missing rate is £0.50."""
    if value is None or value == "":
        return FALLBACK_EX_VAT_RATE
    return _dec(value)


def ex_vat_amount(kwh, rate) -> Decimal:
    """kWh times the ex-VAT rate, half-up to the penny."""
    return to_penny(_dec(kwh) * _dec(rate))


def incl_vat_amount(ex_vat: Decimal) -> Decimal:
    """Add 20% once, half-up to the penny."""
    return to_penny(ex_vat * VAT_FACTOR)


def ex_vat_pence(kwh, rate) -> int:
    """Ex-VAT pence. 12.7 kWh at £0.50 is 635."""
    return int(ex_vat_amount(kwh, rate) * 100)


def incl_vat_pence(kwh, rate) -> int:
    """Card charge in pence. 12.7 kWh at £0.50 ex-VAT is 762."""
    return int(incl_vat_amount(ex_vat_amount(kwh, rate)) * 100)


# £0.30 ex-VAT, once per session, on every charger except the two pilots.
CONNECTION_FEE_PENCE = 30
PILOT_CHARGERS = frozenset({"BR75RN-1", "BR75RN-2"})
# The session stops at £150 ex-VAT. With 20% VAT that is £180.
CAP_EX_VAT_PENCE = 15000
CAP_INCL_VAT_PENCE = 18000


def connection_fee_pence(charge_point: str | None) -> int:
    """£0.30 ex-VAT on every charger except the two pilot chargers."""
    if charge_point in PILOT_CHARGERS:
        return 0
    return CONNECTION_FEE_PENCE


def invoice_total_pence(energy_pence: int, fee_pence: int = 0) -> int:
    """20% once on the ex-VAT sum. 635 + 30 is 798. Lago is the invoice copy of this."""
    ex_vat = Decimal(int(energy_pence) + int(fee_pence)) / Decimal(100)
    return int(incl_vat_amount(ex_vat) * 100)


def session_breakdown(kwh, rate, charge_point: str | None = None) -> dict:
    """Ex-VAT lines for one session, capped at £150 ex-VAT.

    Energy above the cap is a negative overshoot, so the amount due stays
    at £180 including VAT. A session with no energy is all zeros.
    """
    empty = {
        "energy_pence": 0,
        "fee_pence": 0,
        "overshoot_pence": 0,
        "ex_vat_pence": 0,
        "vat_pence": 0,
        "total_pence": 0,
    }
    if _dec(kwh) <= 0:
        return empty
    energy = ex_vat_pence(kwh, rate_or_fallback(rate))
    fee = connection_fee_pence(charge_point)
    gross = energy + fee
    if gross > CAP_EX_VAT_PENCE:
        overshoot = CAP_EX_VAT_PENCE - gross
        ex_vat = CAP_EX_VAT_PENCE
    else:
        overshoot = 0
        ex_vat = gross
    total = invoice_total_pence(ex_vat, 0)
    return {
        "energy_pence": energy,
        "fee_pence": fee,
        "overshoot_pence": overshoot,
        "ex_vat_pence": ex_vat,
        "vat_pence": total - ex_vat,
        "total_pence": total,
    }


def session_cost_pence(kwh, rate, charge_point: str | None = None) -> int:
    """Incl-VAT pence the driver pays. Capped at £180."""
    return session_breakdown(kwh, rate, charge_point)["total_pence"]
