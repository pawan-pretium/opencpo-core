"""
Lago Billing Consumer — one Lago invoice per completed charging session.

Listens for SESSION_CDR events. Each session with energy above zero gets
one CPO invoice, including card and prepaid sessions. OpenCPO calculates the
VAT. Lago stores the same figures and is retried if it is down.

The older usage-event helpers in this file are not called. Calling them
would bill the same session a second time on a subscription.

Consumer group: "lago-billing"
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from events.bus import EventBus
from events.types import EventType
from money import ex_vat_pence, rate_or_fallback

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
    amount_pence            INTEGER,
    line                    TEXT,
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
        await conn.execute(
            "ALTER TABLE ocpp.lago_outbox ADD COLUMN IF NOT EXISTS amount_pence INTEGER"
        )
        await conn.execute(
            "ALTER TABLE ocpp.lago_outbox ADD COLUMN IF NOT EXISTS line TEXT"
        )


async def _resolve_subscription(session_id: str) -> str | None:
    """
    Resolve a session to its Lago external_subscription_id.

    Uses the session's auth_id (OCPP idTag / token uid) → tokens →
    token_groups. Only invoice and direct_debit groups are billable.
    Prepaid and card sessions are not sent. The token group's UUID is
    the subscription id (no external_id column).

    Returns external_subscription_id or None if not billable.
    """
    from state.postgres import db

    try:
        async with db.read() as c:
            row = await c.fetchrow(
                """
                SELECT tg.id AS group_id, tg.billing_method, s.billing_type,
                       s.charge_point, s.start_time, s.stop_time,
                       s.energy_kwh, ps.rate_kwh AS session_rate,
                       cp.metadata->>'tariff_kwh' AS tariff_kwh,
                       cp.metadata->>'display_name' AS display_name,
                       cp.metadata->>'address' AS address,
                       cp.metadata->>'city' AS city,
                       cp.site
                  FROM ocpp.sessions s
                  JOIN ocpp.tokens t ON t.uid = s.auth_id
                  JOIN ocpp.token_groups tg ON tg.id = t.group_id
                  LEFT JOIN ocpp.public_sessions ps
                    ON ps.ocpp_transaction_id = s.transaction_id
                  LEFT JOIN ocpp.charge_points cp ON cp.id = s.charge_point
                 WHERE s.id = $1::uuid
                """,
                session_id,
            )
    except Exception as exc:
        logger.error(
            "Lago subscription lookup failed for session %s: %s",
            session_id,
            exc,
        )
        return None

    if not row:
        return None

    # Card / driver-app sessions are prepaid; do not invoice them again.
    if (row["billing_type"] or "prepaid") == "prepaid":
        return None

    if row["billing_method"] not in ("invoice", "direct_debit"):
        return None

    suffix = os.getenv("LAGO_SUBSCRIPTION_SUFFIX", "fleet_001")
    return f"{row['group_id']}_{suffix}", row


def _british_time(value) -> str:
    """Day, month, year and 24-hour time in London. Example: 4 Oct 2026 10:19."""
    if value is None:
        return "unknown time"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(ZoneInfo("Europe/London"))
    return f"{local.day} {local.strftime('%b %Y %H:%M')}"


def _invoice_line(session_id: str, row) -> str:
    """One invoice line for one session."""
    name = row["display_name"] or row["charge_point"] or "charger"
    place = row["address"] or row["site"] or row["city"] or "site not recorded"
    start = _british_time(row["start_time"])
    end = _british_time(row["stop_time"])
    return (
        f"Energy consumed at the {name}, {place}, "
        f"session {session_id} ({start} to {end})"
    )


async def _send_to_lago(
    transaction_id: str,
    subscription_id: str,
    kwh: float,
    timestamp: str,
    charge_point: str,
    amount_pence: int,
    line: str,
) -> tuple[bool, str]:
    """Send one session to Lago at the ex-VAT amount. Returns (accepted, error_text)."""
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
            "precise_total_amount_cents": str(int(amount_pence)),
            "properties": {
                "kwh": round(float(kwh), 3),
                "charge_point": charge_point or "",
                "line": line,
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
    amount_pence: int,
    line: str,
) -> None:
    """Store a billable event in the outbox until Lago accepts it."""
    from state.postgres import db

    async with db.write() as conn:
        await conn.execute("""
            INSERT INTO ocpp.lago_outbox
                (transaction_id, subscription_id, kwh, event_ts, charge_point,
                 amount_pence, line)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (transaction_id) DO NOTHING
        """, transaction_id, subscription_id, kwh, timestamp, charge_point,
             amount_pence, line)


async def _deliver(row) -> bool:
    """Try to send one outbox row. Remove it on success, else note the error."""
    from state.postgres import db

    accepted, error = await _send_to_lago(
        transaction_id=row["transaction_id"],
        subscription_id=row["subscription_id"],
        kwh=row["kwh"],
        timestamp=row["event_ts"] or "",
        charge_point=row["charge_point"] or "",
        amount_pence=row["amount_pence"] or 0,
        line=row["line"] or "",
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
    """Create any session invoice that Lago has not accepted yet."""
    from lago_invoice import ensure_addons, retry_pending

    while True:
        try:
            await ensure_addons()
            await retry_pending()
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

    from lago_invoice import deliver_session, ensure_addons, ensure_schema, prepare_session

    await ensure_schema()
    # The retry loop starts even when Lago is down, so invoices resume
    # without a process restart.
    asyncio.create_task(_retry_loop(), name="lago-retry")
    try:
        await ensure_addons()
    except Exception as exc:
        logger.warning("Lago add-ons not ready at start: %s", exc)

    async for event in event_bus.consume(
        group=CONSUMER_GROUP,
        consumer=CONSUMER_NAME,
        types={EventType.SESSION_CDR},
    ):
        session_id = event.session_id
        kwh = event.data.get("energy_kwh", 0)

        if event.simulated:
            logger.debug("Skipping simulated CDR: %s", session_id)
            continue

        if kwh <= 0:
            logger.debug("Skipping zero-energy CDR: %s", session_id)
            continue

        prepared = await prepare_session(session_id, kwh)
        if not prepared:
            logger.info(
                "Session %s is not invoiced (%s, %.3f kWh)",
                session_id, event.charge_point, kwh,
            )
            continue
        await deliver_session(session_id)
