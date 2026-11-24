"""One CPO invoice per completed charging session.

OpenCPO calculates the 20% VAT, owns the invoice number, and builds the PDF.
Lago stores the same amounts and the same number. It does not add VAT again
and it does not send the email. If Lago is down, the invoice and the card
payment still complete, and the Lago record is retried.

A registered driver is a row with driver_account_id. Everyone else is a guest.
"""

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx

from money import session_breakdown

logger = logging.getLogger(__name__)

_SKIP_LAGO_MARKER = "skipped:stripe_ledger_prepaid"


def stripe_ledger_for_prepaid() -> bool:
    """True when the pilot flag skips Lago for prepaid/card sessions."""
    from config import config
    return bool(config.stripe_ledger_for_prepaid)


def _is_prepaid_card_invoice(row) -> bool:
    """Driver-app / card path: linked public session, or billing_type prepaid."""
    try:
        public_id = row["public_session_id"]
    except (KeyError, TypeError):
        public_id = None
    if public_id:
        return True
    try:
        billing = row["billing_type"]
    except (KeyError, TypeError):
        billing = None
    return (billing or "").strip().lower() == "prepaid"


def _skip_lago_copy(row) -> bool:
    return stripe_ledger_for_prepaid() and _is_prepaid_card_invoice(row)

LAGO_API_URL = os.getenv("LAGO_API_URL", "http://lago-api:3000").rstrip("/")
ENERGY_ADD_ON = "session_energy"
FEE_ADD_ON = "connection_fee"
OVERSHOOT_ADD_ON = "overshoot_reversal"
VAT_ADD_ON = "vat_amount"
ZERO_TAX = "vat_already_applied"
_INVOICE_START = 14

REGISTERED_TEXT = (
    "Thank you for using Pretium CPO. Your invoice has been emailed to you "
    "and is also available to download from your account."
)
GUEST_TEXT = (
    "Thank you for using Pretium CPO. Register an account with your email "
    "for an improved billing service."
)

_DDL = """
CREATE TABLE IF NOT EXISTS ocpp.session_invoices (
    session_id              TEXT        PRIMARY KEY,
    public_session_id       TEXT,
    customer_external_id    TEXT        NOT NULL,
    driver_email            TEXT,
    driver_phone            TEXT,
    driver_name             TEXT,
    registered              BOOLEAN     NOT NULL DEFAULT FALSE,
    charge_point            TEXT,
    kwh                     REAL        NOT NULL DEFAULT 0,
    rate_pence              INTEGER     NOT NULL DEFAULT 0,
    energy_pence            INTEGER     NOT NULL DEFAULT 0,
    fee_pence               INTEGER     NOT NULL DEFAULT 0,
    total_pence             INTEGER,
    line                    TEXT,
    lago_invoice_id         TEXT,
    want_paid               BOOLEAN     NOT NULL DEFAULT FALSE,
    payment_marked          BOOLEAN     NOT NULL DEFAULT FALSE,
    sms_sent                BOOLEAN     NOT NULL DEFAULT FALSE,
    attempts                INTEGER     NOT NULL DEFAULT 0,
    last_error              TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_ALTERS = (
    "ALTER TABLE ocpp.session_invoices ADD COLUMN IF NOT EXISTS invoice_number TEXT",
    "ALTER TABLE ocpp.session_invoices ADD COLUMN IF NOT EXISTS reference TEXT",
    "ALTER TABLE ocpp.session_invoices ADD COLUMN IF NOT EXISTS overshoot_pence INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE ocpp.session_invoices ADD COLUMN IF NOT EXISTS ex_vat_pence INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE ocpp.session_invoices ADD COLUMN IF NOT EXISTS vat_pence INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE ocpp.session_invoices ADD COLUMN IF NOT EXISTS emailed BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE ocpp.session_invoices ADD COLUMN IF NOT EXISTS lago_paid BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE ocpp.session_invoices ADD COLUMN IF NOT EXISTS lago_mismatch TEXT",
    "ALTER TABLE ocpp.session_invoices ADD COLUMN IF NOT EXISTS bill_address TEXT",
)

_SEQ = """
CREATE TABLE IF NOT EXISTS ocpp.cpo_invoice_seq (
    id              BOOLEAN PRIMARY KEY DEFAULT TRUE,
    next_number     INTEGER NOT NULL,
    CONSTRAINT cpo_invoice_seq_one CHECK (id)
)
"""


def _key() -> str:
    return os.getenv("LAGO_API_KEY", "")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_key()}",
        "Content-Type": "application/json",
    }


def _customer_external_id(row) -> str:
    """One Lago customer per driver. A guest is keyed by email, then phone."""
    account_id = row["driver_account_id"]
    if account_id:
        return f"driver-{account_id}"
    email = (row["driver_email"] or "").strip().lower()
    if email:
        safe = "".join(ch for ch in email if ch.isalnum() or ch in "@._-")
        return f"guest-{safe}"[:180]
    phone = "".join(ch for ch in (row["driver_phone"] or "") if ch.isdigit())
    if phone:
        return f"guest-{phone}"
    return f"session-{row['session_id']}"


def _british_time(value) -> str:
    if value is None:
        return "unknown time"
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(ZoneInfo("Europe/London"))
    return f"{local.day} {local.strftime('%b %Y %H:%M')}"


def _units(kwh) -> str:
    """kWh for the invoice quantity, without a trailing zero."""
    text = format(Decimal(str(kwh)).quantize(Decimal("0.001")), "f")
    return text.rstrip("0").rstrip(".") or "0"


def _energy_line(row) -> str:
    name = row["display_name"] or row["charge_point"] or "charger"
    place = row["address"] or row["site"] or row["city"] or "site not recorded"
    start = _british_time(row["start_time"])
    end = _british_time(row["stop_time"])
    return (
        f"Energy consumed at the {name}, {place}, "
        f"session {row['session_id']} ({start} to {end})"
    )


async def ensure_schema() -> None:
    from state.postgres import db

    async with db.write() as conn:
        await conn.execute(_DDL)
        for statement in _ALTERS:
            await conn.execute(statement)
        await conn.execute(_SEQ)
        await conn.execute(
            """
            INSERT INTO ocpp.cpo_invoice_seq (id, next_number)
            VALUES (TRUE, $1)
            ON CONFLICT DO NOTHING
            """,
            _INVOICE_START,
        )


async def _lago(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    """Call Lago. A network failure returns status 0 and does not raise."""
    if not _key():
        return 0, {"error": "LAGO_API_KEY not configured"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(
                method, f"{LAGO_API_URL}{path}", headers=_headers(), json=payload
            )
    except httpx.HTTPError as exc:
        logger.warning("Lago %s %s failed: %s", method, path, exc)
        return 0, {"error": type(exc).__name__}
    try:
        body = response.json()
    except Exception:
        body = {"error": response.text[:300]}
    if not isinstance(body, dict):
        body = {"data": body}
    return response.status_code, body


_addons_ready = False


async def ensure_addons() -> None:
    """Create the Lago lines once.

    Lago does not email the driver. A one-off invoice email is sent only
    when the Lago licence is premium, and this Lago has no licence. The
    customer is also stored without an email, so there is no recipient.
    email_settings cannot be cleared through the API on this licence.
    """
    global _addons_ready
    if _addons_ready:
        return
    ready = True
    status, _body = await _lago("POST", "/api/v1/taxes", {
        "tax": {
            "name": "VAT already on the CPO invoice",
            "code": ZERO_TAX,
            "rate": "0",
            "description": "OpenCPO has already calculated 20% VAT",
        }
    })
    if status not in (200, 422):
        ready = False
    for code, name in (
        (ENERGY_ADD_ON, "Session energy"),
        (FEE_ADD_ON, "Connection fee"),
        (OVERSHOOT_ADD_ON, "Overshoot reversal"),
        (VAT_ADD_ON, "VAT 20 percent"),
    ):
        status, _body = await _lago("POST", "/api/v1/add_ons", {
            "add_on": {
                "name": name,
                "code": code,
                "amount_cents": 1,
                "amount_currency": "GBP",
                "description": name,
                "tax_codes": [ZERO_TAX],
            }
        })
        if status not in (200, 422):
            ready = False
            logger.warning("Lago add-on %s was not created: HTTP %s", code, status)
    _addons_ready = ready


def _uk_phone(raw: str | None) -> str:
    """Print a UK mobile as +44 7700 900 123. Other numbers are left as given."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if digits.startswith("0") and len(digits) == 11:
        digits = "44" + digits[1:]
    if digits.startswith("44") and len(digits) == 12:
        rest = digits[2:]
        return f"+44 {rest[:4]} {rest[4:7]} {rest[7:]}"
    return (raw or "").strip()


def _bill_to_name(row) -> str:
    """Bill to line: the driver name (or Guest Driver), then the phone used."""
    if row["registered"]:
        who = (row["driver_name"] or "").strip() or (row["driver_email"] or "").strip()
    else:
        who = "Guest Driver"
    phone = _uk_phone(row["driver_phone"])
    return ", ".join(part for part in (who or "Driver", phone) if part)


async def _upsert_customer(row) -> str:
    external_id = row["customer_external_id"]
    customer = {
        "external_id": external_id,
        "name": _bill_to_name(row),
        "currency": "GBP",
        "finalize_zero_amount_invoice": "finalize",
    }
    # Lago's create call updates the customer when the external id exists.
    status, body = await _lago("POST", "/api/v1/customers", {"customer": customer})
    if status not in (200, 201):
        raise RuntimeError(f"Lago customer HTTP {status}: {str(body)[:200]}")
    return external_id


def _fee(code: str, name: str, units: str, amount: int, description: str) -> dict:
    return {
        "add_on_code": code,
        "invoice_display_name": name,
        "description": description,
        "units": units,
        "unit_amount_cents": int(amount),
        "tax_codes": [ZERO_TAX],
    }


def _fees(row) -> list[dict]:
    """The same ex-VAT lines as the CPO invoice, plus the VAT OpenCPO calculated.

    The tax code is 0%. Lago must not add another 20%.
    """
    rate_pence = int(row["rate_pence"] or 0)
    energy_pence = int(row["energy_pence"] or 0)
    if rate_pence or not energy_pence:
        units = _units(row["kwh"])
        unit_amount = rate_pence
    else:
        units = "1"
        unit_amount = energy_pence
    fees = [_fee(ENERGY_ADD_ON, "Energy", units, unit_amount, row["line"] or "Energy")]
    fee_pence = int(row["fee_pence"] or 0)
    if fee_pence:
        fees.append(_fee(FEE_ADD_ON, "Connection fee", "1", fee_pence, "Connection fee, once per session"))
    overshoot = int(row["overshoot_pence"] or 0)
    if overshoot:
        fees.append(_fee(OVERSHOOT_ADD_ON, "Overshoot reversal", "1", overshoot, "Overshoot reversal"))
    vat = int(row["vat_pence"] or 0)
    if vat:
        fees.append(_fee(VAT_ADD_ON, "VAT 20%", "1", vat, "VAT at 20%, calculated by OpenCPO"))
    return fees


async def _create_invoice(row) -> tuple[str, int, str | None]:
    """Send the CPO figures to Lago. Returns lago id, Lago total, and a mismatch note."""
    status, body = await _lago("POST", "/api/v1/invoices", {
        "invoice": {
            "external_customer_id": row["customer_external_id"],
            "currency": "GBP",
            "purchase_order_number": row["invoice_number"],
            "fees": _fees(row),
        }
    })
    if status not in (200, 201):
        raise RuntimeError(f"Lago invoice HTTP {status}: {str(body)[:300]}")
    invoice = body.get("invoice") or {}
    lago_id = invoice.get("lago_id") or ""
    if not lago_id:
        raise RuntimeError("Lago invoice response had no lago_id")
    total = int(invoice.get("total_amount_cents") or 0)
    expected = int(row["total_pence"] or 0)
    notes = []
    if total != expected:
        notes.append(f"Lago total {total}p, CPO total {expected}p")
    if (invoice.get("purchase_order_number") or "") != (row["invoice_number"] or ""):
        notes.append("Lago did not store the CPO invoice number")
    return lago_id, total, "; ".join(notes) or None


async def _send_text(row) -> bool:
    phone = (row["driver_phone"] or "").strip()
    if not phone:
        return False
    from utils import send_sms

    text = REGISTERED_TEXT if row["registered"] else GUEST_TEXT
    await send_sms(phone, text)
    return True


async def prepare_session(session_id: str, kwh: float) -> dict | None:
    """Load one real session and store the ex-VAT lines. None when it is not billed."""
    from state.postgres import db

    async with db.read() as conn:
        row = await conn.fetchrow(
            """
            SELECT s.id::text AS session_id, s.energy_kwh, s.charge_point,
                   s.start_time, s.stop_time, s.simulated,
                   s.metadata->>'unapproved' AS unapproved,
                   ps.id::text AS public_session_id,
                   ps.driver_email, ps.driver_phone,
                   ps.driver_account_id::text AS driver_account_id,
                   ps.rate_kwh AS session_rate,
                   ps.payment_status,
                   da.name AS driver_name,
                   cp.metadata->>'tariff_kwh' AS tariff_kwh,
                   cp.metadata->>'display_name' AS display_name,
                   cp.metadata->>'address' AS address,
                   cp.metadata->>'city' AS city,
                   cp.site
              FROM ocpp.sessions s
              LEFT JOIN ocpp.public_sessions ps
                ON ps.ocpp_transaction_id = s.transaction_id
              LEFT JOIN ocpp.driver_accounts da ON da.id = ps.driver_account_id
              LEFT JOIN ocpp.charge_points cp ON cp.id = s.charge_point
             WHERE s.id = $1::uuid
            """,
            session_id,
        )
    if not row or row["simulated"] or row["unapproved"] == "true":
        return None
    if kwh <= 0:
        return None

    rate = row["session_rate"]
    if rate is None:
        rate = row["tariff_kwh"]
    from money import rate_or_fallback, to_penny

    used_rate = rate_or_fallback(rate)
    lines = session_breakdown(kwh, used_rate, row["charge_point"] or "")
    record = {
        "session_id": row["session_id"],
        "public_session_id": row["public_session_id"],
        "driver_email": row["driver_email"],
        "driver_phone": _uk_phone(row["driver_phone"]),
        "driver_account_id": row["driver_account_id"],
        "driver_name": row["driver_name"] or None,
        "registered": bool(row["driver_account_id"]),
        "charge_point": row["charge_point"],
        "kwh": float(kwh),
        "rate_pence": int(to_penny(used_rate) * 100),
        "energy_pence": lines["energy_pence"],
        "fee_pence": lines["fee_pence"],
        "overshoot_pence": lines["overshoot_pence"],
        "ex_vat_pence": lines["ex_vat_pence"],
        "vat_pence": lines["vat_pence"],
        "total_pence": lines["total_pence"],
        "line": _energy_line(row),
        "want_paid": (row["payment_status"] or "") == "paid",
        "reference": row["public_session_id"] or row["session_id"],
    }
    record["customer_external_id"] = _customer_external_id(record)

    async with db.write() as conn:
        number = await _next_invoice_number(conn)
        record["invoice_number"] = number
        await conn.execute(
            """
            INSERT INTO ocpp.session_invoices (
                session_id, public_session_id, customer_external_id,
                driver_email, driver_phone, driver_name, registered, charge_point,
                kwh, rate_pence, energy_pence, fee_pence, line, want_paid,
                overshoot_pence, ex_vat_pence, vat_pence, total_pence,
                invoice_number, reference
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
            ON CONFLICT (session_id) DO NOTHING
            """,
            record["session_id"], record["public_session_id"],
            record["customer_external_id"], record["driver_email"],
            record["driver_phone"], record["driver_name"], record["registered"],
            record["charge_point"],
            record["kwh"], record["rate_pence"], record["energy_pence"],
            record["fee_pence"],
            record["line"], record["want_paid"],
            record["overshoot_pence"], record["ex_vat_pence"], record["vat_pence"],
            record["total_pence"], record["invoice_number"], record["reference"],
        )
        # The card uses this total even when Lago has not answered yet.
        if row["public_session_id"]:
            await conn.execute(
                """
                UPDATE ocpp.public_sessions
                   SET amount_due_pence = $2
                 WHERE id = $1::uuid
                   AND amount_due_pence IS NULL
                   AND payment_status IN ('authorized', 'settling', 'pending')
                """,
                row["public_session_id"],
                lines["total_pence"],
            )
    return record


async def _next_invoice_number(conn) -> str:
    """The next CPO invoice number. INV-0013 is the approved sample, so this starts at INV-0014."""
    row = await conn.fetchrow(
        """
        UPDATE ocpp.cpo_invoice_seq
           SET next_number = next_number + 1
         WHERE id
        RETURNING next_number - 1 AS n
        """
    )
    number = int(row["n"]) if row and row["n"] is not None else _INVOICE_START
    return f"INV-{number:04d}"


async def deliver_session(session_id: str) -> bool:
    """Finish the CPO invoice, then copy the same figures to Lago. Safe to repeat.

    The email, the text and the paid mark are stored here. A Lago failure
    does not undo them.
    """
    from state.postgres import db

    async with db.read() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM ocpp.session_invoices WHERE session_id = $1",
            session_id,
        )
    if not row:
        return False

    await _send_invoice_email(row)
    if not row["sms_sent"] and (row["driver_phone"] or "").strip():
        try:
            if await _send_text(row):
                async with db.write() as conn:
                    await conn.execute(
                        "UPDATE ocpp.session_invoices SET sms_sent = TRUE WHERE session_id = $1",
                        session_id,
                    )
        except Exception as exc:
            logger.warning("Invoice text failed for %s: %s", session_id, exc)

    total = int(row["total_pence"] or 0)
    if (row["want_paid"] or total == 0) and not row["payment_marked"]:
        async with db.write() as conn:
            await conn.execute(
                """
                UPDATE ocpp.session_invoices
                   SET payment_marked = TRUE, want_paid = TRUE
                 WHERE session_id = $1
                """,
                session_id,
            )

    if _skip_lago_copy(row):
        # Pilot: Stripe is the ledger. Do not create or retry a Lago copy.
        if (row["last_error"] or "") != _SKIP_LAGO_MARKER:
            async with db.write() as conn:
                await conn.execute(
                    """
                    UPDATE ocpp.session_invoices
                       SET last_error = $2
                     WHERE session_id = $1
                    """,
                    session_id, _SKIP_LAGO_MARKER,
                )
            logger.info(
                "Lago skipped for prepaid session %s under STRIPE_LEDGER_FOR_PREPAID",
                session_id,
            )
        return True

    if row["lago_invoice_id"]:
        if (row["want_paid"] or total == 0) and not row["lago_paid"]:
            await _mark_lago_paid(row["lago_invoice_id"], session_id)
        return True

    try:
        await _upsert_customer(row)
        lago_id, lago_total, mismatch = await _create_invoice(row)
    except Exception as exc:
        async with db.write() as conn:
            await conn.execute(
                """
                UPDATE ocpp.session_invoices
                   SET attempts = attempts + 1, last_error = $2
                 WHERE session_id = $1
                """,
                session_id, str(exc)[:300],
            )
        logger.warning("Lago record pending for %s: %s", session_id, exc)
        return False

    async with db.write() as conn:
        await conn.execute(
            """
            UPDATE ocpp.session_invoices
               SET lago_invoice_id = $2, lago_mismatch = $3, last_error = NULL
             WHERE session_id = $1
            """,
            session_id, lago_id, mismatch,
        )
    if mismatch:
        logger.warning("Lago mismatch for %s: %s", row["invoice_number"], mismatch)
    if row["want_paid"] or total == 0:
        await _mark_lago_paid(lago_id, session_id)
    logger.info("CPO invoice %s copied to Lago %s total %s pence", row["invoice_number"], lago_id, lago_total)
    return True


async def _send_invoice_email(row) -> None:
    """Email the CPO PDF from the Pretium CPO mailbox. No address means nothing to send."""
    from state.postgres import db

    if row["emailed"]:
        return
    email = (row["driver_email"] or "").strip()
    if not email:
        return
    try:
        from utils import send_email
        pdf = build_pdf(row)
        total = _pounds(int(row["total_pence"] or 0))
        number = row["invoice_number"] or "invoice"
        extra = ""
        if not row["registered"]:
            extra = (
                "\nRegister an account with your email for an improved billing service.\n"
            )
        sent = await send_email(
            to_email=email,
            subject=f"Pretium invoice {number}",
            body_text=(
                f"Your Pretium tax invoice {number} is attached.\n\n"
                f"Reference: {row['reference'] or row['public_session_id']}\n"
                f"Amount: {total} including 20% VAT\n"
                f"{extra}"
            ),
            attachment_bytes=pdf,
            attachment_name=f"{number}.pdf",
        )
        if not sent:
            return
        async with db.write() as conn:
            await conn.execute(
                "UPDATE ocpp.session_invoices SET emailed = TRUE WHERE session_id = $1",
                row["session_id"],
            )
    except Exception as exc:
        logger.warning("Invoice email failed for %s: %s", row["session_id"], exc)


def _pounds(pence: int) -> str:
    return f"£{int(pence) / 100:.2f}"


def build_pdf(row) -> bytes:
    from cpo_invoice_pdf import build_invoice_pdf
    data = dict(row)
    if not data.get("driver_phone"):
        data["driver_phone"] = ""
    else:
        data["driver_phone"] = _uk_phone(data["driver_phone"])
    return build_invoice_pdf(data)


async def mark_paid_by_session(session_id: str) -> None:
    """The card payment succeeded. The CPO invoice is paid even if Lago is down."""
    from state.postgres import db

    try:
        async with db.read() as conn:
            row = await conn.fetchrow(
                """
                SELECT lago_invoice_id, payment_marked, lago_paid
                  FROM ocpp.session_invoices WHERE session_id = $1
                """,
                session_id,
            )
        if not row:
            return
        if not row["payment_marked"]:
            async with db.write() as conn:
                await conn.execute(
                    """
                    UPDATE ocpp.session_invoices
                       SET payment_marked = TRUE, want_paid = TRUE
                     WHERE session_id = $1
                    """,
                    session_id,
                )
        if row["lago_invoice_id"] and not row["lago_paid"]:
            # Need public_session_id / billing to decide skip; reload minimal fields.
            async with db.read() as conn:
                full = await conn.fetchrow(
                    "SELECT public_session_id FROM ocpp.session_invoices WHERE session_id = $1",
                    session_id,
                )
            if full and _skip_lago_copy(full):
                return
            await _mark_lago_paid(row["lago_invoice_id"], session_id)
    except Exception as exc:
        logger.warning("Paid mark kept locally for %s: %s", session_id, exc)


async def mark_paid_for_public_session(public_session_id: str) -> None:
    """Called when Stripe has taken the money, including a later retry.

    This does not raise. A Lago failure must not make the webhook drop the paid mark.
    """
    from state.postgres import db

    if not public_session_id:
        return
    try:
        async with db.read() as conn:
            row = await conn.fetchrow(
                """
                SELECT session_id, lago_invoice_id, payment_marked, lago_paid
                  FROM ocpp.session_invoices
                 WHERE public_session_id = $1
                """,
                public_session_id,
            )
        if not row:
            return
        if not row["payment_marked"]:
            async with db.write() as conn:
                await conn.execute(
                    """
                    UPDATE ocpp.session_invoices
                       SET payment_marked = TRUE, want_paid = TRUE
                     WHERE session_id = $1
                    """,
                    row["session_id"],
                )
        if row["lago_invoice_id"] and not row["lago_paid"]:
            # Public session = driver card / prepaid path.
            if stripe_ledger_for_prepaid():
                return
            await _mark_lago_paid(row["lago_invoice_id"], row["session_id"])
    except Exception as exc:
        logger.warning("Paid mark kept locally for %s: %s", public_session_id, exc)


async def _mark_lago_paid(lago_id: str, session_id: str) -> None:
    """Tell Lago the invoice is paid. The CPO paid mark is already stored."""
    from state.postgres import db

    status, body = await _lago("PUT", f"/api/v1/invoices/{lago_id}", {
        "invoice": {"payment_status": "succeeded"}
    })
    if status not in (200, 201):
        logger.warning(
            "Lago invoice %s not marked paid: HTTP %s %s",
            lago_id, status, str(body)[:200],
        )
        return
    async with db.write() as conn:
        await conn.execute(
            """
            UPDATE ocpp.session_invoices
               SET lago_paid = TRUE, payment_marked = TRUE, want_paid = TRUE
             WHERE session_id = $1
            """,
            session_id,
        )


async def invoice_pdf(public_session_id: str, account_id: str) -> bytes | None:
    """The CPO PDF for one of this driver's sessions, or None."""
    from state.postgres import db

    async with db.read() as conn:
        row = await conn.fetchrow(
            """
            SELECT si.*
              FROM ocpp.session_invoices si
              JOIN ocpp.public_sessions ps ON ps.id::text = si.public_session_id
             WHERE si.public_session_id = $1
               AND ps.driver_account_id = $2::uuid
            """,
            public_session_id, account_id,
        )
    if not row:
        return None
    return build_pdf(row)


async def invoice_pdf_for_staff(session_id: str) -> bytes | None:
    """The CPO PDF for the admin portal."""
    from state.postgres import db

    async with db.read() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM ocpp.session_invoices WHERE session_id = $1 OR public_session_id = $1",
            session_id,
        )
    if not row:
        return None
    return build_pdf(row)


async def retry_pending() -> None:
    from state.postgres import db

    async with db.read() as conn:
        rows = await conn.fetch(
            """
            SELECT session_id FROM ocpp.session_invoices
             WHERE (
                    lago_invoice_id IS NULL
                    AND COALESCE(last_error, '') <> 'skipped:stripe_ledger_prepaid'
                   )
                OR (
                    payment_marked AND NOT lago_paid
                    AND lago_invoice_id IS NOT NULL
                   )
                OR (NOT emailed AND COALESCE(driver_email, '') <> '')
                OR (NOT sms_sent AND COALESCE(driver_phone, '') <> '')
             ORDER BY created_at
             LIMIT 100
            """
        )
    for row in rows:
        await deliver_session(row["session_id"])
