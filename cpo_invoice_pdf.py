"""The CPO tax invoice. One A4 page. OpenCPO owns this PDF."""

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

_LOGO = Path(__file__).resolve().parent / "assets" / "pretium-logo.png"
_LONDON = ZoneInfo("Europe/London")


def _pounds(pence: int) -> str:
    """Pence as pounds, with a sign for a negative line."""
    sign = "-" if int(pence) < 0 else ""
    return f"{sign}£{abs(int(pence)) / 100:.2f}"


def _date(value) -> str:
    """British date, 15 Nov 2026."""
    if value is None:
        value = datetime.now(timezone.utc)
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(_LONDON)
    return f"{local.day} {local.strftime('%b %Y')}"


def build_invoice_pdf(row) -> bytes:
    """One A4 page for a stored session invoice."""
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setPageCompression(0)
    width, height = A4
    left = 16 * mm
    right = width - 16 * mm
    y = height - 16 * mm

    pdf.setFillColorRGB(0.1, 0.1, 0.1)
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(left, y - 6 * mm, "Tax Invoice")
    if _LOGO.is_file():
        pdf.drawImage(
            str(_LOGO), right - 22 * mm, y - 14 * mm,
            width=18 * mm, height=18 * mm, mask="auto", preserveAspectRatio=True,
        )
    y -= 22 * mm

    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(left, y, "Bill to")
    pdf.drawRightString(right, y, "Pretium Synergy Ltd")
    y -= 4.5 * mm
    pdf.setFont("Helvetica", 9)
    bill = [row.get("driver_name") or ("Guest Driver" if not row.get("registered") else "Driver")]
    if row.get("driver_email"):
        bill.append(row["driver_email"])
    if row.get("driver_phone"):
        bill.append(row["driver_phone"])
    # A guest has no postal address. A registered driver shows one only when we hold it.
    if row.get("registered") and row.get("bill_address"):
        bill.append(row["bill_address"])
    from_lines = [
        "11 Old Bond Street",
        "London",
        "England",
        "W1S 4PN",
        "VAT GB371441315",
    ]
    for index in range(max(len(bill), len(from_lines))):
        if index < len(bill):
            pdf.drawString(left, y, str(bill[index])[:70])
        if index < len(from_lines):
            pdf.drawRightString(right, y, from_lines[index])
        y -= 4.2 * mm

    y -= 3 * mm
    pdf.setStrokeColorRGB(0.8, 0.8, 0.8)
    pdf.line(left, y, right, y)
    y -= 6 * mm
    paid = bool(row.get("payment_marked"))
    total = int(row.get("total_pence") or 0)
    due = 0 if paid else total
    meta = [
        ("Amount due", _pounds(due)),
        ("Issue date", _date(row.get("created_at"))),
        ("Invoice number", row.get("invoice_number") or ""),
        ("Reference", str(row.get("reference") or row.get("public_session_id") or "")),
    ]
    col = left
    span = (right - left) / 4
    for label, value in meta:
        pdf.setFont("Helvetica", 7)
        pdf.setFillColorRGB(0.35, 0.35, 0.35)
        pdf.drawString(col, y, label)
        pdf.setFillColorRGB(0.1, 0.1, 0.1)
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(col, y - 4 * mm, str(value)[:36])
        col += span
    y -= 10 * mm

    pdf.setFillColorRGB(0.95, 0.95, 0.95)
    pdf.rect(left, y - 2 * mm, right - left, 6 * mm, fill=1, stroke=0)
    pdf.setFillColorRGB(0.2, 0.2, 0.2)
    pdf.setFont("Helvetica-Bold", 8)
    headers = [("Description", left + 1 * mm), ("Quantity", right - 78 * mm),
               ("Price", right - 52 * mm), ("Tax", right - 32 * mm), ("Amount", right - 2 * mm)]
    for label, x in headers:
        if label == "Amount":
            pdf.drawRightString(x, y, label)
        else:
            pdf.drawString(x, y, label)
    y -= 7 * mm
    pdf.setFillColorRGB(0.1, 0.1, 0.1)
    pdf.setFont("Helvetica", 8)

    def line(description, quantity, price_pence, amount_pence):
        nonlocal y
        pdf.drawString(left + 1 * mm, y, description[:68])
        pdf.drawString(right - 78 * mm, y, quantity)
        pdf.drawRightString(right - 36 * mm, y, _pounds(price_pence))
        pdf.drawString(right - 32 * mm, y, "20%")
        pdf.drawRightString(right - 2 * mm, y, _pounds(amount_pence))
        y -= 5 * mm

    kwh = row.get("kwh")
    if kwh:
        number = f"{float(kwh):.3f}".rstrip("0").rstrip(".")
        quantity = f"{number} kWh"
    else:
        quantity = "0 kWh"
    line(row.get("line") or "Energy", quantity, int(row.get("rate_pence") or 0), int(row.get("energy_pence") or 0))
    fee = int(row.get("fee_pence") or 0)
    if fee:
        line("Connection fee", "1", fee, fee)
    overshoot = int(row.get("overshoot_pence") or 0)
    if overshoot:
        line("Overshoot reversal", "1", overshoot, overshoot)

    y -= 2 * mm
    pdf.line(right - 70 * mm, y + 3 * mm, right, y + 3 * mm)
    ex_vat = int(row.get("ex_vat_pence") or 0)
    vat = int(row.get("vat_pence") or 0)
    totals = [("Subtotal", _pounds(ex_vat)), ("Total VAT 20%", _pounds(vat)), ("Total", _pounds(total))]
    if paid:
        totals.append(("Less amount paid", _pounds(total)))
    totals.append(("Amount due", _pounds(due)))
    for label, value in totals:
        pdf.setFont("Helvetica-Bold" if label in ("Total", "Amount due") else "Helvetica", 8)
        pdf.drawString(right - 70 * mm, y, label)
        pdf.drawRightString(right, y, value)
        y -= 4.5 * mm

    pdf.setFont("Helvetica", 8)
    pdf.setFillColorRGB(0.25, 0.25, 0.25)
    footer = "https://pretiumsynergy.co.uk/    CPO ID GB*007    eMSP ID GB-007"
    pdf.drawString(left, 12 * mm, footer)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
