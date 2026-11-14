"""Stripe billing for a driver session.

The driver pays a £5 card check on Stripe's page before the charger starts.
That check is released at the end. The card saved during the check is then
charged for the energy, at the rate shown on the charge screen, up to £100.
A charge under 30p is not sent to the bank. A failed final charge is tried
three times in total, then the driver is emailed a payment link and their
next session is refused until that link is paid.
"""
import asyncio
import logging
import os
from decimal import Decimal, ROUND_HALF_UP

import httpx

logger = logging.getLogger(__name__)

HOLD_PENCE = 500
SESSION_CAP_PENCE = 10000
MIN_CHARGE_PENCE = 30
CHARGE_ATTEMPTS = 3
_API = "https://api.stripe.com/v1"


class StripeCallError(Exception):
    """Stripe refused a call. The message is Stripe's own, with no secrets."""

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def _app_url() -> str:
    return os.getenv("CHARGE_APP_URL", "https://app.cpo.pretiumsynergy.co.uk").rstrip("/")


def session_amount_pence(kwh, rate_kwh) -> int:
    """Price in pence at the rate the driver was shown, capped at £100."""
    amount = (Decimal(str(kwh or 0)) * Decimal(str(rate_kwh or 0))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    pence = int(amount * 100)
    if pence < 0:
        return 0
    return min(pence, SESSION_CAP_PENCE)


async def _stripe(method: str, path: str, data: dict | None = None) -> dict:
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key:
        raise StripeCallError(0, "Card payments are not configured")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.request(
            method, _API + path, data=data or {}, auth=(key, "")
        )
    body = response.json()
    if response.status_code >= 400:
        err = body.get("error", {}) if isinstance(body, dict) else {}
        raise StripeCallError(response.status_code, err.get("message", "Stripe refused the call"))
    return body


async def create_card_check(session_id: str, cp_id: str, connector_id: int) -> str:
    """Open Stripe's page for the £5 card check. Returns the page URL."""
    base = _app_url()
    session = await _stripe("POST", "/checkout/sessions", {
        "mode": "payment",
        "customer_creation": "always",
        "success_url": f"{base}/session/{session_id}",
        "cancel_url": f"{base}/charge/{cp_id}/{connector_id}",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "gbp",
        "line_items[0][price_data][unit_amount]": str(HOLD_PENCE),
        "line_items[0][price_data][product_data][name]": "Pretium card check (£5, released after charging)",
        "payment_intent_data[capture_method]": "manual",
        "payment_intent_data[setup_future_usage]": "off_session",
        "payment_intent_data[metadata][opencpo_session_id]": session_id,
        "payment_intent_data[metadata][kind]": "hold",
        "metadata[opencpo_session_id]": session_id,
    })
    url = session.get("url") or ""
    if not url:
        raise StripeCallError(502, "Stripe did not return a payment page")
    return url


async def cancel_hold(payment_intent_id: str | None) -> None:
    """Release the £5 check. A hold that is already gone is not an error."""
    if not payment_intent_id:
        return
    try:
        await _stripe("POST", f"/payment_intents/{payment_intent_id}/cancel", {})
    except StripeCallError as exc:
        logger.info("Hold %s not cancelled: %s", payment_intent_id, exc)


async def debt_is_paid(checkout_id: str | None) -> bool:
    """True when the driver has paid the link for an earlier session."""
    if not checkout_id:
        return False
    try:
        session = await _stripe("GET", f"/checkout/sessions/{checkout_id}")
    except StripeCallError as exc:
        logger.warning("Could not check payment link %s: %s", checkout_id, exc)
        return False
    return session.get("payment_status") == "paid"


async def _charge_saved_card(row, pence: int) -> dict:
    return await _stripe("POST", "/payment_intents", {
        "amount": str(pence),
        "currency": "gbp",
        "customer": row["stripe_customer_id"],
        "payment_method": row["stripe_payment_method_id"],
        "off_session": "true",
        "confirm": "true",
        "description": f"Pretium charging {row['cp_id']}",
        "metadata[opencpo_session_id]": row["id"],
        "metadata[kind]": "final",
    })


async def _email_payment_link(row, pence: int, url: str) -> None:
    email = row["driver_email"]
    if not email or not url:
        return
    pounds = f"£{pence / 100:.2f}"
    try:
        from utils import send_email
        await send_email(
            to_email=email,
            subject="Pretium charging payment",
            body_text=(
                f"Your charging session could not be charged to your card.\n\n"
                f"Amount due: {pounds}\n"
                f"Pay here: {url}\n\n"
                f"You will not be able to start another session until this is paid.\n"
            ),
        )
    except Exception as exc:
        logger.warning("Payment email failed for session %s: %s", row["id"][:8], exc)


async def settle_session(transaction_id: int) -> None:
    """Charge the saved card for the energy once the charger has stopped."""
    from state.postgres import db

    async with db.write() as conn:
        row = await conn.fetchrow("""
            UPDATE ocpp.public_sessions
               SET payment_status = 'settling'
             WHERE ocpp_transaction_id = $1
               AND payment_status = 'authorized'
            RETURNING id::text AS id, cp_id, driver_email, rate_kwh, kwh_delivered,
                      stripe_customer_id, stripe_payment_method_id, external_payment_id
        """, transaction_id)
    if not row:
        return

    pence = session_amount_pence(row["kwh_delivered"], row["rate_kwh"])
    if pence < MIN_CHARGE_PENCE:
        await cancel_hold(row["external_payment_id"])
        async with db.write() as conn:
            await conn.execute("""
                UPDATE ocpp.public_sessions
                   SET payment_status = 'paid', amount_due_pence = $2
                 WHERE id = $1::uuid
            """, row["id"], pence)
        return
    if not row["stripe_payment_method_id"]:
        async with db.write() as conn:
            await conn.execute("""
                UPDATE ocpp.public_sessions
                   SET payment_status = 'unpaid', amount_due_pence = $2
                 WHERE id = $1::uuid
            """, row["id"], pence)
        logger.warning("Session %s has no saved card; left unpaid", row["id"][:8])
        return

    payment_id = None
    for attempt in range(1, CHARGE_ATTEMPTS + 1):
        try:
            payment = await _charge_saved_card(row, pence)
        except StripeCallError as exc:
            logger.warning("Final charge attempt %s failed for %s: %s", attempt, row["id"][:8], exc)
            payment = None
        if payment and payment.get("status") == "succeeded":
            payment_id = payment.get("id")
            break
        if attempt < CHARGE_ATTEMPTS:
            await asyncio.sleep(20)

    if payment_id:
        await cancel_hold(row["external_payment_id"])
        async with db.write() as conn:
            await conn.execute("""
                UPDATE ocpp.public_sessions
                   SET payment_status = 'paid', final_payment_id = $2, amount_due_pence = $3
                 WHERE id = $1::uuid
            """, row["id"], payment_id, pence)
        logger.info("Session %s charged %s pence", row["id"][:8], pence)
        return

    link = await _payment_link_full(row, pence)
    async with db.write() as conn:
        await conn.execute("""
            UPDATE ocpp.public_sessions
               SET payment_status = 'unpaid', amount_due_pence = $2,
                   payment_url = $3, debt_checkout_id = $4
             WHERE id = $1::uuid
        """, row["id"], pence, link["url"], link["id"])
    await _email_payment_link(row, pence, link["url"])
    logger.info("Session %s left unpaid, link emailed", row["id"][:8])


async def _payment_link_full(row, pence: int) -> dict:
    """Stripe page for the balance. Returns id and url, or blanks if Stripe refuses."""
    base = _app_url()
    try:
        session = await _stripe("POST", "/checkout/sessions", {
            "mode": "payment",
            "customer": row["stripe_customer_id"],
            "success_url": f"{base}/receipt/{row['id']}",
            "cancel_url": f"{base}/session/{row['id']}",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": "gbp",
            "line_items[0][price_data][unit_amount]": str(pence),
            "line_items[0][price_data][product_data][name]": "Pretium charging",
            "metadata[opencpo_session_id]": row["id"],
            "metadata[kind]": "debt",
            "payment_intent_data[metadata][opencpo_session_id]": row["id"],
            "payment_intent_data[metadata][kind]": "debt",
        })
    except StripeCallError as exc:
        logger.warning("Payment link failed for %s: %s", row["id"][:8], exc)
        return {"id": None, "url": ""}
    return {"id": session.get("id"), "url": session.get("url") or ""}
