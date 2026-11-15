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
