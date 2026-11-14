"""
Lago Billing Consumer — sends charging events to Lago for invoicing.

Listens for SESSION_CDR events on the event bus and forwards the kWh
delivered to Lago as billable events. Lago handles invoice generation,
tax calculation, and payment collection.

Durability: the event bus acknowledges a message once the consumer loop
moves on, so a failed Lago send must not rely on the bus for retry. Every
billable event is written to ocpp.lago_outbox first, removed only after
Lago accepts it, and resent by a retry loop. Lago deduplicates on
transaction_id, so a resend is safe.

Consumer group: "lago-billing"
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx

from events.bus import EventBus
from events.types import EventType

logger = logging.getLogger(__name__)

# Configuration
LAGO_API_URL = os.getenv("LAGO_API_URL", "https://api.getlago.com")
LAGO_API_KEY = os.getenv("LAGO_API_KEY", "")
CONSUMER_GROUP = "lago-billing"
CONSUMER_NAME = "lago-billing-worker-1"
RETRY_INTERVAL_S = 60
RETRY_BATCH = 100

_OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS ocpp.lago_outbox (
    transaction_id          TEXT        PRIMARY KEY,
    subscription_id         TEXT        NOT NULL,
    kwh                     REAL        NOT NULL,
    event_ts                TEXT,
    charge_point            TEXT,
    attempts                INTEGER     NOT NULL DEFAULT 0,
    last_error              TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


async def _ensure_outbox() -> None:
    """Create the outbox table on first run. Safe to repeat."""
    from state.postgres import db

    async with db.write() as conn:
        await conn.execute(_OUTBOX_DDL)


async def _resolve_subscription(charge_point: str) -> str | None:
    """
    Resolve a charge point to its Lago external_subscription_id.

    Returns external_subscription_id or None if not billable (no group
    with a billing method).
    """
    from state.postgres import db

    async with db.read() as c:
        row = await c.fetchrow("""
            SELECT g.external_id
            FROM ocpp.charge_points cp
            JOIN ocpp.groups g ON cp.group_id = g.id
            WHERE cp.id = $1 AND g.billing_method IS NOT NULL
        """, charge_point)

    if row and row["external_id"]:
        return f"{row['external_id']}_{os.getenv('LAGO_SUBSCRIPTION_SUFFIX', 'fleet_001')}"

    return None


async def _send_to_lago(
    transaction_id: str,
    subscription_id: str,
    kwh: float,
    timestamp: str,
    charge_point: str,
) -> tuple[bool, str]:
    """Send one billable event to Lago. Returns (accepted, error_text)."""
    if not LAGO_API_KEY:
        return False, "LAGO_API_KEY not configured"

    payload = {
        "event": {
            "transaction_id": transaction_id,
            "external_subscription_id": subscription_id,
            "code": "kwh_delivered",
            "timestamp": int(datetime.fromisoformat(timestamp).timestamp())
            if timestamp
            else int(datetime.now(timezone.utc).timestamp()),
            "properties": {
                "kwh": round(kwh, 3),
            },
        }
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{LAGO_API_URL}/api/v1/events",
                headers={
                    "Authorization": f"Bearer {LAGO_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

        if resp.status_code == 200:
            lago_event = resp.json().get("event", {})
            logger.info(
                f"Billing event sent to Lago: {transaction_id} | "
                f"{kwh:.3f} kWh | sub={subscription_id} | "
                f"lago_id={lago_event.get('lago_id', '?')}"
            )
            return True, ""
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"

    except httpx.TimeoutException:
        return False, "timeout"
    except Exception as e:
        return False, str(e)[:300]


async def _record(
    transaction_id: str,
    subscription_id: str,
    kwh: float,
    timestamp: str,
    charge_point: str,
) -> None:
    """Store a billable event in the outbox until Lago accepts it."""
    from state.postgres import db

    async with db.write() as conn:
        await conn.execute("""
            INSERT INTO ocpp.lago_outbox
                (transaction_id, subscription_id, kwh, event_ts, charge_point)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (transaction_id) DO NOTHING
        """, transaction_id, subscription_id, kwh, timestamp, charge_point)


async def _deliver(row) -> bool:
    """Try to send one outbox row. Remove it on success, else note the error."""
    from state.postgres import db

    accepted, error = await _send_to_lago(
        transaction_id=row["transaction_id"],
        subscription_id=row["subscription_id"],
        kwh=row["kwh"],
        timestamp=row["event_ts"] or "",
        charge_point=row["charge_point"] or "",
    )
    async with db.write() as conn:
        if accepted:
            await conn.execute(
                "DELETE FROM ocpp.lago_outbox WHERE transaction_id = $1",
                row["transaction_id"],
            )
        else:
            await conn.execute("""
                UPDATE ocpp.lago_outbox
                   SET attempts = attempts + 1, last_error = $2
                 WHERE transaction_id = $1
            """, row["transaction_id"], error)
            logger.warning(
                "Lago delivery pending for %s: %s", row["transaction_id"], error
            )
    return accepted


async def _retry_loop() -> None:
    """Resend every pending outbox row on a fixed interval."""
    from state.postgres import db

    while True:
        try:
            async with db.read() as conn:
                rows = await conn.fetch("""
                    SELECT transaction_id, subscription_id, kwh, event_ts, charge_point
                      FROM ocpp.lago_outbox
                  ORDER BY created_at
                     LIMIT $1
                """, RETRY_BATCH)
            for row in rows:
                await _deliver(row)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Lago retry loop error: %s", exc)
        await asyncio.sleep(RETRY_INTERVAL_S)


async def run(event_bus: EventBus) -> None:
    """
    Main consumer loop. Listens for SESSION_CDR events, stores each billable
    one in the outbox, and delivers it to Lago.
    """
    logger.info(
        f"Lago billing consumer starting | "
        f"api={LAGO_API_URL} | group={CONSUMER_GROUP}"
    )

    if not LAGO_API_KEY:
        logger.warning(
            "LAGO_API_KEY not set — billable sessions are held in ocpp.lago_outbox "
            "until the key is added."
        )

    await _ensure_outbox()
    asyncio.create_task(_retry_loop(), name="lago-retry")

    async for event in event_bus.consume(
        group=CONSUMER_GROUP,
        consumer=CONSUMER_NAME,
        types={EventType.SESSION_CDR},
    ):
        session_id = event.session_id
        charge_point = event.charge_point
        kwh = event.data.get("energy_kwh", 0)

        if event.simulated:
            logger.debug(f"Skipping simulated CDR: {session_id}")
            continue

        if kwh <= 0:
            logger.debug(f"Skipping zero-energy CDR: {session_id}")
            continue

        subscription_id = await _resolve_subscription(charge_point)
        if not subscription_id:
            logger.info(
                f"No billable subscription for {charge_point} — "
                f"session {session_id} ({kwh:.3f} kWh) not billed"
            )
            continue

        # Transaction ID = session_id (idempotent — Lago deduplicates)
        await _record(
            transaction_id=session_id,
            subscription_id=subscription_id,
            kwh=kwh,
            timestamp=event.timestamp,
            charge_point=charge_point,
        )
        await _deliver({
            "transaction_id": session_id,
            "subscription_id": subscription_id,
            "kwh": kwh,
            "event_ts": event.timestamp,
            "charge_point": charge_point,
        })
