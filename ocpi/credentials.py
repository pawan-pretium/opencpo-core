"""
OCPI 2.2.1 Credentials module — partner registration + token exchange.
"""
import logging
import os
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Depends

from ocpi.main import ocpi_response, ocpi_error, verify_ocpi_token
from state.postgres import db

logger = logging.getLogger(__name__)
router = APIRouter()


async def _get_ocpi_identity() -> dict:
    """Return OCPI identity from settings DB, falling back to env vars."""
    try:
        from state.settings import get_setting
        s = await get_setting("ocpi")
    except Exception:
        s = {}
    return {
        "country_code":  s.get("country_code")  or os.getenv("OCPI_COUNTRY_CODE", "NL"),
        "party_id":      s.get("party_id")       or os.getenv("OCPI_PARTY_ID", "OCP"),
        "role":          s.get("role")            or "CPO",
        "operator_name": s.get("operator_name")  or os.getenv("OCPI_OPERATOR_NAME", "OpenCPO"),
        "base_url":      s.get("base_url")        or os.getenv("OCPI_BASE_URL", "http://localhost:8000"),
    }


async def _cpo_role_info() -> dict:
    """Build our CPO role information from settings (with env-var fallback)."""
    identity = await _get_ocpi_identity()
    return {
        "role": identity["role"],
        "business_details": {
            "name":    identity["operator_name"],
            "website": os.getenv("OCPI_OPERATOR_WEBSITE", ""),
        },
        "party_id":     identity["party_id"],
        "country_code": identity["country_code"],
    }


@router.get("")
async def get_credentials(token: str = Depends(verify_ocpi_token)):
    """Return our credentials to the partner."""
    identity = await _get_ocpi_identity()
    return ocpi_response({
        "token": token,
        "url": f"{identity['base_url']}/ocpi/versions",
        "roles": [await _cpo_role_info()],
    })


@router.post("")
async def post_credentials(request: Request):
    """
    Partner sends their credentials — we register them and return ours.
    This is the OCPI handshake.

    OCPI 2.2.1 requires the partner to present the initial token we issued
    out-of-band (see api/ocpi_management.py create_partner, which stores it
    as token_a with status 'pending'). Without that token anyone could
    register as an active partner and read sessions and CDRs.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Token "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    initial_token = auth[6:].strip()

    async with db.read() as conn:
        pending = await conn.fetchrow(
            "SELECT id FROM ocpp.ocpi_partners WHERE token_a = $1 AND status = 'pending'",
            initial_token,
        )
    if not pending:
        raise HTTPException(401, "Invalid OCPI token")

    body = await request.json()

    partner_token = body.get("token", "")
    partner_url = body.get("url", "")
    roles = body.get("roles", [])

    if not partner_token or not partner_url or not roles:
        return ocpi_error(2001, "Missing required fields: token, url, roles")

    role = roles[0]
    party_id = role.get("party_id", "")
    country_code = role.get("country_code", "")
    partner_role = role.get("role", "EMSP")
    name = role.get("business_details", {}).get("name", "Unknown")

    # Generate our token for them
    our_token = secrets.token_urlsafe(32)

    # Activate the partner row the operator created. The initial token is
    # spent here: token_a now holds our new token for this partner.
    async with db.write() as conn:
        await conn.execute("""
            UPDATE ocpp.ocpi_partners SET
                party_id = $2, country_code = $3, role = $4, name = $5, url = $6,
                token_a = $7, token_b = $8, status = 'active', last_sync = NOW()
            WHERE id = $1
        """, pending["id"], party_id, country_code, partner_role, name, partner_url,
            our_token, partner_token)

    logger.info(f"OCPI credentials exchanged: {country_code}*{party_id} ({name}) role={partner_role}")

    identity = await _get_ocpi_identity()
    return ocpi_response({
        "token": our_token,
        "url": f"{identity['base_url']}/ocpi/versions",
        "roles": [await _cpo_role_info()],
    })


@router.put("")
async def put_credentials(request: Request, token: str = Depends(verify_ocpi_token)):
    """Update partner credentials (re-registration)."""
    body = await request.json()
    roles = body.get("roles", [{}])
    role = roles[0]

    new_token = body.get("token", "")
    new_url = body.get("url", "")

    async with db.write() as conn:
        await conn.execute("""
            UPDATE ocpp.ocpi_partners SET
                token_b = $1, url = $2, last_sync = NOW()
            WHERE token_a = $3
        """, new_token, new_url, token)

    identity = await _get_ocpi_identity()
    return ocpi_response({
        "token": token,
        "url": f"{identity['base_url']}/ocpi/versions",
        "roles": [await _cpo_role_info()],
    })


@router.delete("")
async def delete_credentials(token: str = Depends(verify_ocpi_token)):
    """Partner unregisters."""
    async with db.write() as conn:
        await conn.execute(
            "UPDATE ocpp.ocpi_partners SET status = 'suspended' WHERE token_a = $1", token
        )
    logger.info("OCPI partner unregistered")
    return ocpi_response()



