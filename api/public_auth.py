"""
Public OTP authentication endpoints.
Drivers authenticate with phone number + 6-digit OTP stored in Redis.
"""
import json
import logging
import os
import random
import string
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from state.redis import redis_state
from utils import send_email as _send_email
from utils import send_sms as _send_sms

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/public", tags=["public-auth"])

class OtpRequest(BaseModel):
    phone: str


class OtpVerify(BaseModel):
    phone: str
    code: str


class EmailOtpRequest(BaseModel):
    email: str


class EmailOtpVerify(BaseModel):
    email: str
    code: str


async def _is_demo_mode() -> bool:
    """Return True if OTP demo_mode setting is enabled."""
    from state.settings import get_setting
    otp_cfg = await get_setting("otp")
    return otp_cfg.get("demo_mode", False)


async def _get_otp_config() -> dict:
    """Return OTP settings with safe defaults."""
    from state.settings import get_setting
    otp_cfg = await get_setting("otp")
    return {
        "enabled":      otp_cfg.get("enabled", True),
        "code_length":  int(otp_cfg.get("code_length", 6)),
        "ttl_seconds":  int(otp_cfg.get("ttl_seconds", 300)),
    }


@router.post("/auth/send-otp")
async def send_otp(req: OtpRequest):
    """Send OTP code to driver's phone. Stored in Redis with configured TTL."""
    phone = req.phone.strip().replace(" ", "")
    if len(phone) < 7:
        raise HTTPException(400, "Invalid phone number")

    otp_cfg = await _get_otp_config()
    if not otp_cfg["enabled"]:
        raise HTTPException(503, "OTP authentication is disabled")

    code_len = otp_cfg["code_length"]
    ttl      = otp_cfg["ttl_seconds"]
    code     = "".join(random.choices(string.digits, k=code_len))

    otp_data = json.dumps({
        "code":     code,
        "attempts": 0,
        "created":  datetime.now(timezone.utc).isoformat(),
    })
    await redis_state.set(f"otp:{phone}", otp_data, ttl=ttl)

    demo = await _is_demo_mode()
    if demo:
        logger.info("OTP demo mode for %s — code stored, will not send SMS", phone[-4:])
        return {"phone": phone, "sent": True, "demo_mode": True}

    operator_name = os.getenv("OPERATOR_NAME", "Pretium")
    sms_ok = await _send_sms(phone, f"{operator_name}: your verification code is {code}. Valid for {ttl // 60} minutes.")
    if not sms_ok:
        await redis_state.delete(f"otp:{phone}")
        logger.warning("OTP for %s: SMS failed", phone[-4:])
        raise HTTPException(502, "We could not send a text message. Use email instead.")
    logger.info("OTP for %s: sent via SMS", phone[-4:])

    return {"phone": phone, "sent": True, "demo_mode": False}


@router.post("/auth/verify-otp")
async def verify_otp(req: OtpVerify):
    """Verify OTP code and return a session token."""
    phone = req.phone.strip().replace(" ", "")
    raw   = await redis_state.get(f"otp:{phone}")

    if not raw:
        raise HTTPException(400, "No code found. Request a new one.")

    stored = json.loads(raw)
    stored["attempts"] += 1

    if stored["attempts"] > 5:
        await redis_state.client.delete(f"otp:{phone}")
        raise HTTPException(429, "Too many attempts. Request a new code.")

    code_match = (req.code == stored["code"])

    if not code_match:
        await redis_state.set(f"otp:{phone}", json.dumps(stored), ttl=300)
        raise HTTPException(400, "Incorrect code")

    await redis_state.client.delete(f"otp:{phone}")
    token = str(uuid4())
    return {"token": token, "phone": phone}


def _normalise_email(email: str) -> str:
    """Lower-case and trim an email address."""
    return email.strip().lower()


def _email_ok(email: str) -> bool:
    """Reject addresses that cannot receive a verification code."""
    if len(email) < 6 or len(email) > 254 or " " in email:
        return False
    local, sep, domain = email.partition("@")
    return bool(sep and local and "." in domain and not domain.startswith("."))


@router.post("/auth/send-email-otp")
async def send_email_otp(req: EmailOtpRequest):
    """Send a 6-digit code to the driver's email address."""
    email = _normalise_email(req.email)
    if not _email_ok(email):
        raise HTTPException(400, "Invalid email address")

    otp_cfg = await _get_otp_config()
    if not otp_cfg["enabled"]:
        raise HTTPException(503, "Verification is disabled")

    code_len = otp_cfg["code_length"]
    ttl = otp_cfg["ttl_seconds"]
    code = "".join(random.choices(string.digits, k=code_len))
    otp_data = json.dumps({
        "code": code,
        "attempts": 0,
        "created": datetime.now(timezone.utc).isoformat(),
    })
    key = f"otp:email:{email}"
    await redis_state.set(key, otp_data, ttl=ttl)

    operator_name = os.getenv("OPERATOR_NAME", "Pretium")
    minutes = max(1, ttl // 60)
    sent = await _send_email(
        email,
        f"{operator_name} verification code",
        (
            f"Your {operator_name} verification code is {code}.\n\n"
            f"It is valid for {minutes} minutes.\n\n"
            "If you did not ask for this code, you can ignore this email."
        ),
    )
    if not sent:
        await redis_state.delete(key)
        logger.warning("Email OTP failed for %s", email.split("@")[-1])
        raise HTTPException(502, "We could not send the email. Please try again.")

    logger.info("Email OTP sent to domain %s", email.split("@")[-1])
    return {"email": email, "sent": True}


@router.post("/auth/verify-email-otp")
async def verify_email_otp(req: EmailOtpVerify):
    """Check the email code and return a session token."""
    email = _normalise_email(req.email)
    key = f"otp:email:{email}"
    raw = await redis_state.get(key)

    if not raw:
        raise HTTPException(400, "No code found. Request a new one.")

    stored = json.loads(raw)
    stored["attempts"] += 1

    if stored["attempts"] > 5:
        await redis_state.delete(key)
        raise HTTPException(429, "Too many attempts. Request a new code.")

    if req.code != stored["code"]:
        await redis_state.set(key, json.dumps(stored), ttl=300)
        raise HTTPException(400, "Incorrect code")

    await redis_state.delete(key)
    token = str(uuid4())
    return {"token": token, "email": email}
