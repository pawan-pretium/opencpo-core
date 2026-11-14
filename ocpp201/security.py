"""
OCPP 2.0.1 Security & ISO 15118 / Plug & Charge handlers.

Mixin for ChargePointHandler201 — covers:
- SignCertificate (charger SECC cert CSR)
- Get15118EVCertificate (contract cert provisioning via charger relay)
- SecurityEventNotification (tamper, cert, firmware audit events)
- _validate_contract_cert helper (P&C eMAID auth)
"""
import logging
import time
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from events.types import Event, EventType
from iso15118.contract import authorize_contract, handle_ev_certificate
from pki.ca import ca
from state.postgres import db
from ocpp201.protocol import AuthorizationStatus, OCPPMessage, ServerAction

logger = logging.getLogger(__name__)


class SecurityMixin:
    """Mixin providing security, PKI, and ISO 15118 handlers."""

    async def _on_security_event(self, payload: dict) -> dict:
        """Security event from charger — log + publish."""
        event_type = payload.get("type", "")
        timestamp = payload.get("timestamp", datetime.now(timezone.utc).isoformat())
        tech_info = payload.get("techInfo", "")

        severity = "critical" if "Tamper" in event_type or "InvalidCertificate" in event_type else "warning"

        async with db.write() as conn:
            await conn.execute("""
                INSERT INTO ocpp.security_events (time, charge_point, event_type, severity, details)
                VALUES ($1, $2, $3, $4, $5)
            """, timestamp, self.cp_id, event_type, severity, {"tech_info": tech_info})

        await self.event_bus.publish(Event(
            type=EventType.OPS_ALERT,
            charge_point=self.cp_id,
            simulated=self._simulated,
            data={"security_event": event_type, "severity": severity, "tech_info": tech_info},
        ))

        logger.warning(f"[{self.cp_id}] Security event: {event_type} ({severity})")
        return {}

    def _queue_call(self, action: str, payload: dict) -> None:
        """Queue a call to the charger, sent after the current response."""
        message = OCPPMessage.call(action, payload)
        self._pending_calls[message.unique_id] = (action, time.monotonic())
        self._outbound.append(message.to_json())

    def _csr_to_pem(self, csr_text: str) -> str:
        text = (csr_text or "").strip()
        if "BEGIN CERTIFICATE REQUEST" in text:
            return text if text.endswith("\n") else text + "\n"
        import base64
        der = base64.b64decode(text)
        request = x509.load_der_x509_csr(der)
        return request.public_bytes(Encoding.PEM).decode()

    async def _on_sign_certificate(self, payload: dict) -> dict:
        """Sign the charger's certificate request and return the chain."""
        csr_raw = payload.get("csr", "")
        cert_type = payload.get("certificateType", "ChargingStationCertificate")

        async with db.write() as conn:
            log_id = await conn.fetchval(
                """
                INSERT INTO ocpp.pki_csr_log (charge_point, csr_pem, status)
                VALUES ($1, $2, 'pending')
                RETURNING id
                """,
                self.cp_id, csr_raw,
            )

        try:
            csr_pem = self._csr_to_pem(csr_raw)
            cert_pem, serial_hex = await ca.sign_secc_csr(csr_pem, self.cp_id)
        except Exception as exc:
            logger.warning("[%s] SignCertificate rejected: %s", self.cp_id, exc)
            async with db.write() as conn:
                await conn.execute(
                    """
                    UPDATE ocpp.pki_csr_log
                    SET status = 'rejected', processed_at = NOW()
                    WHERE id = $1
                    """,
                    log_id,
                )
            return {"status": "Rejected"}

        async with db.write() as conn:
            await conn.execute(
                """
                UPDATE ocpp.pki_csr_log
                SET status = 'signed', cert_serial = $1, processed_at = NOW()
                WHERE id = $2
                """,
                serial_hex, log_id,
            )

        chain = "".join([
            cert_pem if cert_pem.endswith("\n") else cert_pem + "\n",
            ca._cpo_sub_ca_cert.public_bytes(Encoding.PEM).decode(),
            ca._root_ca_cert.public_bytes(Encoding.PEM).decode(),
        ])
        self._queue_call(ServerAction.CERTIFICATE_SIGNED, {
            "certificateChain": chain,
            "certificateType": cert_type,
        })
        logger.info("[%s] SignCertificate signed serial=%s", self.cp_id, serial_hex)
        await self.event_bus.publish(Event(
            type=EventType.PKI_CERT_ISSUED,
            charge_point=self.cp_id,
            simulated=self._simulated,
            data={"type": cert_type, "status": "signed", "serial": serial_hex},
        ))
        return {"status": "Accepted"}

    async def _on_get_15118_ev_certificate(self, payload: dict) -> dict:
        """Install or update the vehicle contract certificate."""
        action = payload.get("action", "Install")
        schema = payload.get("iso15118SchemaVersion", "")
        logger.info(
            "[%s] Get15118EVCertificate: action=%s schema=%s",
            self.cp_id, action, schema,
        )
        return await handle_ev_certificate(payload, self.cp_id)

    async def _validate_contract_cert(self, emaid: str, cert_hash_data: list) -> str:
        """Check the vehicle contract certificate against the CA."""
        return await authorize_contract(emaid, cert_hash_data)
