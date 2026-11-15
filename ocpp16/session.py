"""OCPP 1.6 session lifecycle — StartTransaction, StopTransaction, CDR generation."""
import logging
from decimal import Decimal, ROUND_HALF_UP

import httpx

from config import config
from money import rate_or_fallback, session_breakdown, session_cost_pence
from events.bus import EventBus
from events.types import Event, EventType
from state.postgres import db
from state.redis import redis_state
from ocpp16.protocol import AuthorizationStatus
from ocpp16.meter import session_energy
from utils import parse_timestamp

logger = logging.getLogger(__name__)


async def handle_start_transaction(
    cp_id: str,
    event_bus: EventBus,
    simulated: bool,
    payload: dict,
    billing_context: dict | None = None,
) -> dict:
    """Charging session started."""
    connector_id = payload.get("connectorId", 1)
    id_tag = payload.get("idTag", "")
    meter_start = payload.get("meterStart", 0)
    timestamp = parse_timestamp(payload.get("timestamp"))

    billing_type = (billing_context or {}).get("type", "prepaid")
    billing_group_id = (billing_context or {}).get("group_id")

    # Create session in DB
    async with db.transaction() as conn:
        session = await conn.fetchrow("""
            INSERT INTO ocpp.sessions 
                (charge_point, connector_id, auth_method, auth_id, start_time, simulated, meter_start, billing_type, billing_group_id)
            VALUES ($1, $2, 'rfid', $3, $4, $5, $6, $7, $8)
            RETURNING id, id::text as session_id
        """, cp_id, connector_id, id_tag, timestamp, simulated, float(meter_start), billing_type, billing_group_id)

        # Generate a transaction ID (OCPP 1.6 requires integer)
        transaction_id = await conn.fetchval(
            "UPDATE ocpp.sessions SET transaction_id = (SELECT COALESCE(MAX(transaction_id), 0) + 1 FROM ocpp.sessions) WHERE id = $1 RETURNING transaction_id",
            session["id"],
        )

    session_id = session["session_id"]

    # Update Redis
    await redis_state.set_session(session_id, {
        "charge_point": cp_id,
        "connector_id": str(connector_id),
        "auth_id": id_tag,
        "transaction_id": str(transaction_id),
        "start_time": timestamp,
        "meter_start": str(meter_start),
        "energy_kwh": "0",
        "power_kw": "0",
        "status": "active",
    })

    # Publish event
    await event_bus.publish(Event(
        type=EventType.SESSION_START,
        charge_point=cp_id,
        connector=connector_id,
        session_id=session_id,
        simulated=simulated,
        data={
            "transaction_id": transaction_id,
            "id_tag": id_tag,
            "meter_start": meter_start,
        },
    ))

    logger.info(f"[{cp_id}] Session started: txn={transaction_id} tag={id_tag} connector={connector_id}")

    # Link to public session if APP_ tag (charge app payment flow)
    if id_tag.startswith("APP_"):
        public_session_prefix = id_tag[4:].lower()  # APP_88FBC7B6 → 88fbc7b6
        async with db.write() as conn:
            await conn.execute("""
                UPDATE ocpp.public_sessions SET
                    ocpp_transaction_id = $1, started_at = NOW()
                WHERE LEFT(id::text, 8) = $2
                  AND ocpp_transaction_id IS NULL
                  AND started_at IS NULL
                  AND id = (
                      SELECT id FROM ocpp.public_sessions
                       WHERE LEFT(id::text, 8) = $2
                         AND ocpp_transaction_id IS NULL
                         AND started_at IS NULL
                       ORDER BY created_at DESC
                       LIMIT 1
                  )
            """, transaction_id, public_session_prefix)
        logger.info(f"[{cp_id}] Linked public session {public_session_prefix}* to txn={transaction_id}")

    return {
        "transactionId": transaction_id,
        "idTagInfo": {"status": AuthorizationStatus.ACCEPTED},
    }


async def handle_stop_transaction(
    cp_id: str,
    event_bus: EventBus,
    simulated: bool,
    payload: dict,
) -> dict:
    """Charging session stopped."""
    transaction_id = payload.get("transactionId")
    meter_stop = payload.get("meterStop", 0)
    timestamp = parse_timestamp(payload.get("timestamp"))
    reason = payload.get("reason", "Local")
    id_tag = payload.get("idTag", "")

    # ⚠️ MAXPOWER quirk: sends StopTransaction with reason="Other" on reconnect
    # Don't finalize session if connector still shows Charging after reconnect
    # For now: log it, process normally, flag for review if reason="Other"
    if reason == "Other":
        logger.warning(f"[{cp_id}] StopTransaction reason='Other' (possible reconnect artifact)")

    # Find session, compute final energy via session_energy (single formula)
    session = None
    async with db.transaction() as conn:
        row = await conn.fetchrow(
            "SELECT id::text as session_id, COALESCE(meter_start, 0) as meter_start "
            "FROM ocpp.sessions WHERE charge_point = $1 AND transaction_id = $2 AND status = 'active'",
            cp_id, transaction_id,
        )
        if row:
            final_energy = session_energy(float(meter_stop) / 1000.0, float(row["meter_start"]))
            session = await conn.fetchrow("""
                UPDATE ocpp.sessions SET
                    status = 'completed',
                    stop_time = $1,
                    stop_reason = $2,
                    meter_stop = $3,
                    energy_kwh = $4
                WHERE charge_point = $5 AND transaction_id = $6 AND status = 'active'
                RETURNING id::text as session_id, energy_kwh, start_time, meter_start
            """, timestamp, reason, float(meter_stop), final_energy, cp_id, transaction_id)

    if session:
        session_id = session["session_id"]

        # Update public_sessions.stopped_at + kwh_delivered (charge app receipt flow)
        async with db.write() as conn:
            await conn.execute("""
                UPDATE ocpp.public_sessions
                SET stopped_at = $1, kwh_delivered = $2
                WHERE ocpp_transaction_id = $3 AND stopped_at IS NULL
            """, timestamp, final_energy, transaction_id)

        # Clean up Redis
        await redis_state.del_session(session_id)

        # Generate CDR
        await generate_cdr(cp_id, event_bus, simulated, session_id)

        # Publish event
        await event_bus.publish(Event(
            type=EventType.SESSION_STOP,
            charge_point=cp_id,
            session_id=session_id,
            simulated=simulated,
            data={
                "transaction_id": transaction_id,
                "reason": reason,
                "energy_kwh": float(session["energy_kwh"]),
                "meter_stop": meter_stop,
            },
        ))

        logger.info(f"[{cp_id}] Session stopped: txn={transaction_id} reason={reason} energy={session['energy_kwh']:.1f}kWh")

        # Charge the saved card for the energy. Runs after the charger is answered.
        import asyncio
        from api.stripe_billing import settle_session
        asyncio.get_running_loop().create_task(settle_session(transaction_id))

        # Send push notification if driver subscribed
        await _notify_session_complete(session_id, float(session["energy_kwh"]), transaction_id)
    else:
        logger.warning(f"[{cp_id}] StopTransaction for unknown/inactive txn={transaction_id}")

    return {"idTagInfo": {"status": AuthorizationStatus.ACCEPTED}} if id_tag else {}


async def _notify_session_complete(
    ocpp_session_id: str,
    energy_kwh: float,
    transaction_id: int,
) -> None:
    """Send push notification + receipt email to driver on session completion (best-effort)."""
    try:
        # Look up public session by ocpp_transaction_id to get rate and public session id
        async with db.read() as conn:
            row = await conn.fetchrow("""
                SELECT id::text as public_id, cp_id, rate_kwh, driver_email, driver_phone,
                       driver_account_id
                FROM ocpp.public_sessions
                WHERE ocpp_transaction_id = $1
                LIMIT 1
            """, transaction_id)

        if not row:
            return  # No public session (e.g. RFID session) — skip

        public_id = row["public_id"]
        # Same figure as the screen, receipt and invoice: fee added once, incl. VAT.
        cost = session_cost_pence(energy_kwh, rate_or_fallback(row["rate_kwh"]), row["cp_id"]) / 100

        # Push notification
        title = "Charging complete"
        body = f"{energy_kwh:.2f} kWh charged — £{cost:.2f} including 20% VAT"
        url = f"/receipt/{public_id}"

        # Push notification via standalone push service
        push_url = config.push.service_url
        if push_url:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(
                        f"{push_url}/api/v1/push/send",
                        json={"session_id": public_id, "title": title, "body": body, "url": url},
                        headers={"X-API-Key": config.push.api_key},
                    )
            except Exception as e:
                logger.warning("Push service call failed: %s", e)

        try:
            from api.push import send_push_for_session
            await send_push_for_session(public_id, title, body, url)
        except Exception as e:
            logger.warning("In-process push failed: %s", e)

    except Exception as e:
        logger.warning(f"Session completion notification failed for {ocpp_session_id}: {e}")


def _calculate_cdr_cost(
    energy_kwh: float,
    tariff_rate: Decimal = Decimal("0.50"),
    charge_point: str | None = None,
) -> dict:
    """The capped invoice total: energy, the connection fee, overshoot, and 20% VAT."""
    lines = session_breakdown(energy_kwh, tariff_rate, charge_point)
    return {
        "energy_kwh": float(energy_kwh),
        "rate_kwh": float(tariff_rate),
        "energy_cost": lines["energy_pence"] / 100,
        "time_cost": 0.0,
        "flat_fee": lines["fee_pence"] / 100,
        "overshoot": lines["overshoot_pence"] / 100,
        "subtotal": lines["ex_vat_pence"] / 100,
        "vat_rate": 20.0,
        "vat_amount": lines["vat_pence"] / 100,
        "total": lines["total_pence"] / 100,
        "currency": "GBP",
    }


async def generate_cdr(
    cp_id: str,
    event_bus: EventBus,
    simulated: bool,
    session_id: str,
) -> None:
    """Generate a Charge Detail Record for a completed session."""
    import json
    async with db.write() as c:
        session = await c.fetchrow("""
            SELECT s.charge_point, s.connector_id, s.auth_method, s.auth_id,
                   s.start_time, s.stop_time, s.energy_kwh,
                   ps.rate_kwh AS session_rate,
                   cp.metadata->>'tariff_kwh' AS tariff_kwh
              FROM ocpp.sessions s
              LEFT JOIN ocpp.public_sessions ps
                ON ps.ocpp_transaction_id = s.transaction_id
              LEFT JOIN ocpp.charge_points cp ON cp.id = s.charge_point
             WHERE s.id::text = $1
        """, session_id)

        if not session:
            return

        duration_min = 0
        if session["stop_time"] and session["start_time"]:
            delta = session["stop_time"] - session["start_time"]
            duration_min = delta.total_seconds() / 60

        # Calculate cost with Decimal arithmetic
        try:
            stored_rate = session["session_rate"]
            if stored_rate is None:
                stored_rate = session["tariff_kwh"]
            cost_json = _calculate_cdr_cost(
                float(session["energy_kwh"]),
                rate_or_fallback(stored_rate),
                session["charge_point"],
            )
        except Exception as exc:
            logger.warning(f"[{cp_id}] Failed to calculate CDR cost for session {session_id}: {exc}")
            cost_json = None

        await c.execute("""
            INSERT INTO ocpp.cdrs 
                (session_id, charge_point, connector_id, auth_method, auth_id,
                 start_time, stop_time, energy_kwh, duration_min, cost)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """,
            session_id, session["charge_point"], session["connector_id"],
            session["auth_method"], session["auth_id"],
            session["start_time"], session["stop_time"],
            session["energy_kwh"], duration_min,
            json.dumps(cost_json) if cost_json is not None else None,
        )

        # Publish CDR event
        await event_bus.publish(Event(
            type=EventType.SESSION_CDR,
            charge_point=cp_id,
            session_id=session_id,
            simulated=simulated,
            data={
                "energy_kwh": float(session["energy_kwh"]),
                "duration_min": round(duration_min, 1),
                "cost_total": cost_json["total"] if cost_json else None,
            },
        ))
