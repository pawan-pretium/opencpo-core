"""
Public driver-facing API — core endpoints.
QR lookup, charger discovery, payment webhook, cert identity, and management.
OTP auth: see api/public_auth.py
Session management: see api/public_sessions.py
"""
import hashlib
import hmac
import json
import logging
import os
import asyncio
from datetime import datetime, timezone
import time

from fastapi import APIRouter, HTTPException, Request, Response
from typing import Optional

from state.postgres import db
from state import charger_registry
from state.redis import redis_state

logger = logging.getLogger(__name__)

router         = APIRouter(prefix="/api/v1/public", tags=["public"])
webhook_router = APIRouter(tags=["payments"])


async def _get_charger_live(cp_id: str) -> dict | None:
    try:
        return await redis_state.get_charger(cp_id)
    except Exception:
        return None


# ── QR Lookup ────────────────────────────────────────────────────────────

@router.get("/qr/{code}/lookup")
async def qr_lookup(code: str):
    """Resolve a QR sticker code to a charger + connector."""
    return {
        "cp_id":       code,
        "connector_id": 1,
        "site_id":     "",
        "evse_id":     "",
    }


# ── Charger Discovery ────────────────────────────────────────────────────

@router.get("/chargers/nearby")
async def chargers_nearby(
    lat: float = 0.0, lng: float = 0.0, radius: float = 50,
    include_simulated: bool = True,
):
    """Return chargers with location data, enriched with live status from Redis."""
    sim_filter = "" if include_simulated else "AND simulated = false"
    async with db.read() as conn:
        rows = await conn.fetch(f"""
            SELECT id, vendor, model, status,
                   metadata->>'display_name' AS display_name,
                   metadata->>'address' AS address,
                   metadata->>'city' AS city,
                   (metadata->>'latitude')::float AS latitude,
                   (metadata->>'longitude')::float AS longitude,
                   (metadata->>'max_power_kw')::float AS max_power_kw,
                   (metadata->>'tariff_kwh')::float AS tariff_kwh
              FROM ocpp.charge_points
             WHERE (metadata->>'access_type' = 'public' OR metadata->>'access_type' IS NULL)
               AND metadata->>'latitude' IS NOT NULL
               AND metadata->>'longitude' IS NOT NULL
               {sim_filter}
             ORDER BY id
        """)

    chargers = []
    for r in rows:
        live = await _get_charger_live(r["id"])
        connectors = []
        if live:
            for key, val in live.items():
                if key.startswith("connector_") and key.endswith("_status"):
                    conn_id = int(key.split("_")[1])
                    if conn_id > 0:
                        connectors.append({"id": conn_id, "type": "CCS2", "status": val})

        energy_rate = float(r["tariff_kwh"]) if r["tariff_kwh"] else 0.35
        chargers.append({
            "id": r["id"],
            "name": r["display_name"] or r["id"],
            "address": r["address"] or "",
            "city": r["city"] or "",
            "lat": float(r["latitude"]),
            "lng": float(r["longitude"]),
            "power_kw": r["max_power_kw"] or 0,
            "connectors": connectors,
            "status": live.get("status", "offline") if live else "offline",
            "operator": os.getenv("OPERATOR_NAME", "Your CPO"),
            "tariff_kwh": energy_rate,
            "energy_rate": energy_rate,
            "time_rate": 0.00,
            "idle_rate": 0.05,
            "flat_fee": 0.00,
        })

    return {"chargers": chargers, "total": len(chargers)}


# ── Payment Webhook (Stripe) ─────────────────────────────────────────────
# Only Stripe may change payment state. Every request must carry a valid
# Stripe-Signature header made with STRIPE_WEBHOOK_SECRET. With no secret
# configured, every request is rejected. There is no unsigned path.

STRIPE_WEBHOOK_TOLERANCE_S = 300

# Stripe event type -> payment_status written on the public session.
_STRIPE_STATUS = {
    "payment_intent.amount_capturable_updated": "authorized",
    "payment_intent.payment_failed": "failed",
    "payment_intent.canceled": "cancelled",
}


def _verify_stripe_signature(payload: bytes, header: str, secret: str) -> bool:
    """Check a Stripe-Signature header (t=...,v1=...) against the raw body."""
    if not secret or not header:
        return False
    timestamp = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    if not timestamp or not timestamp.isdigit() or not signatures:
        return False
    if abs(time.time() - int(timestamp)) > STRIPE_WEBHOOK_TOLERANCE_S:
        return False
    signed = timestamp.encode() + b"." + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in signatures)


@webhook_router.post("/api/payments/webhook")
async def payment_webhook(request: Request):
    """
    Stripe webhook for the driver hold.

    A bad or missing signature returns 400 and changes nothing. A valid
    event returns 200 even when no session matches, so Stripe does not retry.
    """
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    payload = await request.body()
    if not _verify_stripe_signature(payload, request.headers.get("stripe-signature", ""), secret):
        logger.warning("Payment webhook: rejected request (missing or bad Stripe signature)")
        raise HTTPException(status_code=400, detail="invalid signature")

    try:
        event = json.loads(payload)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid json")
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="invalid event")

    status = _STRIPE_STATUS.get(event.get("type", ""))
    obj = (event.get("data") or {}).get("object") or {}
    if not isinstance(obj, dict):
        return {"ok": True}
    metadata = obj.get("metadata") or {}
    kind = metadata.get("kind", "hold")
    session_id = metadata.get("opencpo_session_id", "")
    payment_intent_id = obj.get("id", "")

    # A final charge and a payment-link charge are settled by the API call
    # that created them, or by the next visit. Do not let those events
    # restart a charger or cancel a session that already ran.
    if kind in ("final", "debt"):
        if event.get("type") == "payment_intent.succeeded" and session_id:
            async with db.write() as conn:
                await conn.execute("""
                    UPDATE ocpp.public_sessions
                       SET payment_status = 'paid', final_payment_id = $2
                     WHERE id = $1::uuid AND payment_status IN ('unpaid', 'settling')
                """, session_id, payment_intent_id)
        return {"ok": True}

    if status is None or not payment_intent_id:
        return {"ok": True}

    async with db.read() as conn:
        if session_id:
            row = await conn.fetchrow("""
                SELECT id::text AS id, cp_id, connector_id, payment_status
                  FROM ocpp.public_sessions WHERE id = $1::uuid
            """, session_id)
        else:
            row = await conn.fetchrow("""
                SELECT id::text AS id, cp_id, connector_id, payment_status
                  FROM ocpp.public_sessions WHERE external_payment_id = $1
            """, payment_intent_id)
    if not row:
        logger.warning("Payment webhook: no session for payment_intent=%s", payment_intent_id)
        return {"ok": True}

    session_id = row["id"]
    prev_status = row["payment_status"]

    # Releasing the £5 check after the session has started must not cancel it.
    if status == "cancelled" and prev_status in ("authorized", "paid", "settling", "unpaid"):
        return {"ok": True}
    if status == "failed" and prev_status in ("authorized", "paid", "settling", "unpaid"):
        return {"ok": True}

    customer = obj.get("customer") if isinstance(obj.get("customer"), str) else None
    method = obj.get("payment_method") if isinstance(obj.get("payment_method"), str) else None

    async with db.write() as conn:
        await conn.execute("""
            UPDATE ocpp.public_sessions
               SET payment_status = $2,
                   external_payment_id = COALESCE($3, external_payment_id),
                   stripe_customer_id = COALESCE($4, stripe_customer_id),
                   stripe_payment_method_id = COALESCE($5, stripe_payment_method_id)
             WHERE id = $1::uuid
        """, session_id, status, payment_intent_id, customer, method)

    logger.info("Session %s payment_status → %s (%s)", session_id[:8], status, event.get("type"))

    # Start the charger only once, and only when the card can be charged again later.
    if status == "authorized" and prev_status not in ("authorized", "paid"):
        if customer and method:
            await _trigger_remote_start(session_id, row["cp_id"], row["connector_id"])
        else:
            logger.warning("Session %s card check has no saved card; charger not started", session_id[:8])

    return {"ok": True}


async def _trigger_remote_start(session_id: str, cp_id: str, connector_id: int) -> None:
    """Send RemoteStartTransaction to the charger after successful payment.

    If the charger is not currently connected (reboot window), the command is
    queued in Redis (TTL 120s). It will be retried when the charger reconnects.
    """
    id_tag = f"APP_{session_id[:8].upper()}"
    msg_id = await charger_registry.send_remote_start(cp_id, connector_id, id_tag)

    if msg_id:
        logger.info(
            "RemoteStartTransaction sent: session=%s cp=%s connector=%d id_tag=%s",
            session_id[:8], cp_id, connector_id, id_tag,
        )
    else:
        logger.warning(
            "RemoteStart queued — charger %s not connected (session=%s). "
            "Will retry when charger reconnects.",
            cp_id, session_id[:8],
        )

        async with db.read() as conn:
            row = await conn.fetchrow(
                "SELECT external_payment_id FROM ocpp.public_sessions WHERE id = $1::uuid",
                session_id,
            )
        external_payment_id = row["external_payment_id"] if row else None

        await redis_state.set(
            f"pending_start:{cp_id}:{session_id}",
            json.dumps({
                "session_id": session_id,
                "cp_id": cp_id,
                "connector_id": connector_id,
                "id_tag": id_tag,
                "external_payment_id": external_payment_id,
                "retries": 0,
            }),
            ttl=120,
        )
        asyncio.get_running_loop().create_task(_release_if_never_started(session_id))


async def _release_if_never_started(session_id: str) -> None:
    """Release the £5 check if the charger has not started within three minutes."""
    await asyncio.sleep(180)
    async with db.read() as conn:
        row = await conn.fetchrow("""
            SELECT payment_status, ocpp_transaction_id, external_payment_id
              FROM ocpp.public_sessions WHERE id = $1::uuid
        """, session_id)
    if not row or row["ocpp_transaction_id"] or row["payment_status"] != "authorized":
        return
    from api.stripe_billing import cancel_hold
    await cancel_hold(row["external_payment_id"])
    async with db.write() as conn:
        await conn.execute("""
            UPDATE ocpp.public_sessions
               SET payment_status = 'cancelled', stopped_at = NOW()
             WHERE id = $1::uuid AND ocpp_transaction_id IS NULL
        """, session_id)
    logger.info("Released card check for session %s; charger did not start", session_id[:8])


async def _fail_pending_start(data: dict) -> None:
    """Mark a pending public session as failed when RemoteStart retries are exhausted."""
    session_id = data.get("session_id", "")
    if not session_id:
        return

    async with db.write() as conn:
        await conn.execute("""
            UPDATE ocpp.public_sessions
               SET payment_status = 'remote_start_failed'
             WHERE id = $1::uuid
               AND started_at IS NULL
        """, session_id)

    logger.warning("Session %s marked as remote_start_failed (retries exhausted)", session_id[:8])


# ── Management endpoints for public sessions (API key required) ──────────

mgmt_public_router = APIRouter(tags=["Public Sessions (Management)"])


@mgmt_public_router.get("/receipts")
async def list_receipt_sessions(limit: int = 100, offset: int = 0):
    """List completed public sessions eligible for receipts."""
    async with db.read() as conn:
        rows = await conn.fetch("""
            SELECT ps.id::text AS id, ps.cp_id, ps.connector_id,
                   ps.driver_phone, ps.driver_email,
                   ps.kwh_delivered, ps.rate_kwh, ps.started_at, ps.stopped_at,
                   ps.payment_status, ps.pricing_tier,
                   cp.metadata->>'display_name' AS display_name,
                   cp.metadata->>'address' AS address,
                   cp.metadata->>'city' AS city
              FROM ocpp.public_sessions ps
              LEFT JOIN ocpp.charge_points cp ON cp.id = ps.cp_id
             WHERE ps.stopped_at IS NOT NULL AND ps.kwh_delivered > 0
             ORDER BY ps.stopped_at DESC
             LIMIT $1 OFFSET $2
        """, limit, offset)
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM ocpp.public_sessions WHERE stopped_at IS NOT NULL AND kwh_delivered > 0"
        )
    return {
        "sessions": [
            {
                "id": r["id"],
                "cp_id": r["cp_id"],
                "charger_name": r["display_name"] or r["cp_id"],
                "address": r["address"] or "",
                "city": r["city"] or "",
                "driver_phone": r["driver_phone"] or "",
                "driver_email": r["driver_email"] or "",
                "kwh": round(float(r["kwh_delivered"]), 2),
                "rate_kwh": round(float(r["rate_kwh"] or 0), 4),
                "cost_incl": round(float(r["kwh_delivered"]) * float(r["rate_kwh"] or 0) * 1.21, 2),
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "stopped_at": r["stopped_at"].isoformat() if r["stopped_at"] else None,
                "payment_status": r["payment_status"],
                "pricing_tier": r["pricing_tier"] or "public",
            }
            for r in rows
        ],
        "total": count,
    }


# ── Certificate Identity ─────────────────────────────────────────────────

@router.get("/cert/identify")
async def cert_identify(serial: str):
    """
    Resolve a client cert serial to a driver account.

    Returns the driver's account info + pricing tier if the cert is valid
    and linked to a driver_account. Returns 404 if cert is unknown, revoked,
    or not linked to any account.
    """
    if not serial or not serial.strip():
        raise HTTPException(status_code=400, detail="Missing serial")

    serial = serial.strip()

    # Caddy sends serial as decimal integer, we store as lowercase hex
    if serial.isdigit():
        serial = hex(int(serial))[2:].lower()
    else:
        serial = serial.lower()

    async with db.read() as conn:
        cert_row = await conn.fetchrow("""
            SELECT serial, subject, not_after
            FROM ocpp.pki_certificates
            WHERE LOWER(serial) = $1
              AND type = 'user'
              AND status = 'active'
              AND revoked_at IS NULL
        """, serial)

        if not cert_row:
            raise HTTPException(status_code=404, detail="Unknown or revoked certificate")

        # Extract email from CN= in subject DN
        subject = cert_row["subject"]
        cert_email = None
        for part in subject.split(","):
            part = part.strip()
            if part.upper().startswith("CN="):
                cert_email = part[3:]
                break

        if not cert_email:
            raise HTTPException(status_code=404, detail="Certificate has no CN email")

        row = await conn.fetchrow("""
            SELECT id, email, name, pricing_tier, group_id, phone, language
            FROM ocpp.driver_accounts
            WHERE email = $1
        """, cert_email)

    if not row:
        raise HTTPException(status_code=404, detail="Certificate not linked to a driver account")

    return {
        "id": str(row["id"]),
        "email": row["email"],
        "name": row["name"],
        "phone": row["phone"],
        "language": row["language"],
        "pricing_tier": row["pricing_tier"],
        "group_id": str(row["group_id"]) if row["group_id"] else None,
        "cert_serial": cert_row["serial"],
        "cert_expires_at": cert_row["not_after"].isoformat() if cert_row["not_after"] else None,
    }
