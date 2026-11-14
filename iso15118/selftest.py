"""Prove Plug and Charge can issue a contract certificate and read it back.

Run inside the core container:

    python -m iso15118.selftest
"""
import asyncio
import base64
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID
from expy.v2gjson import iso2
from expy.v2gjson.common import exi_signed_t

from iso15118.contract import decrypt_contract_key, handle_ev_certificate
from iso15118.exi_codec import processor
from pki.ca import ca
from state.postgres import db


async def main() -> None:
    await db.connect()
    await ca.initialize()

    oem_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Self test OEM")])
    oem = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(oem_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=2))
        .sign(oem_key, hashes.SHA256())
    )
    root_id = iso2.X509IssuerSerialType(
        "CN=SelfTest",
        exi_signed_t({"octets": [1], "octets_count": 1}, 0),
    )
    request = iso2.CertificateInstallationReqType(
        "ID1",
        bytearray(oem.public_bytes(Encoding.DER)),
        iso2.ListOfRootCertificateIDsType([root_id]),
    )
    encoded = processor().encode_fragment(iso2.exiFragment(CertificateInstallationReq=request))
    result = await handle_ev_certificate(
        {
            "action": "Install",
            "iso15118SchemaVersion": "urn:iso:15118:2:2013:MsgDef",
            "exiRequest": base64.b64encode(encoded).decode(),
        },
        "SELFTEST",
    )
    if result.get("status") != "Accepted" or not result.get("exiResponse"):
        raise SystemExit(f"certificate install failed: {result}")

    decoded = processor().decode(base64.b64decode(result["exiResponse"]))
    body = decoded["Body"]["CertificateInstallationRes"]
    if body["ResponseCode"] != 0:
        raise SystemExit(f"response code {body['ResponseCode']}")
    encrypted = bytes(body["ContractSignatureEncryptedPrivateKey"]["CONTENT"]["bytes"])
    dh_public = bytes(body["DHpublickey"]["CONTENT"]["bytes"])
    opened = decrypt_contract_key(encrypted, oem_key, dh_public)
    if len(opened) != 32:
        raise SystemExit(f"decrypted key length {len(opened)}")

    emaid_chars = body["eMAID"]["CONTENT"]["characters"]
    emaid = bytes(emaid_chars).decode()
    async with db.write() as conn:
        await conn.execute(
            "DELETE FROM ocpp.pki_certificates WHERE type = 'contract' AND subject = $1",
            emaid,
        )
        await conn.execute("DELETE FROM ocpp.authorization_cache WHERE token = $1", emaid)
    print(f"Plug and Charge certificate install ok emaid={emaid}")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
