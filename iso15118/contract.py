"""Plug and Charge contract certificates.

A charger asks for a vehicle certificate with Get15118EVCertificate.
This module reads that request, signs a contract certificate with the
MO sub-CA, encrypts the private key to the vehicle's OEM certificate,
and returns the EXI response.

Authorisation checks that certificate against the same CA and the
revocation record. It does not stop at a token list.
"""
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.concatkdf import ConcatKDFHash
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat,
)
from cryptography.x509 import ocsp
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

from config import config
from iso15118.exi_codec import (
    decode_request,
    encode_response,
    field_bytes,
    field_text,
    find_key,
)
from ocpp201.protocol import AuthorizationStatus
from pki.ca import ca

logger = logging.getLogger(__name__)

RESPONSE_OK = 0
RESPONSE_FAILED = 4
RESPONSE_EXPIRED = 10
RESPONSE_NO_CERT = 12
RESPONSE_REVOKED = 25

_HASHES = {
    "SHA256": hashes.SHA256(),
    "SHA384": hashes.SHA384(),
    "SHA512": hashes.SHA512(),
}


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(Encoding.PEM).decode()


def _der(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(Encoding.DER)


def sa_provisioning_key():
    """Private key of the SA provisioning leaf. ISO 15118-20 signs with it."""
    path = ca.data_dir / "sa-provisioning.key"
    if not path.exists():
        ensure_sa_provisioning_cert()
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def ensure_sa_provisioning_cert() -> x509.Certificate:
    """Leaf certificate the vehicle sees as the operator provisioning cert.

    The key is written beside the certificate (mode 0600). A certificate
    without its key cannot sign, so an older certificate with no key is
    reissued together with a new key.
    """
    path = ca.data_dir / "sa-provisioning.crt"
    key_path = ca.data_dir / "sa-provisioning.key"
    if path.exists() and key_path.exists():
        return x509.load_pem_x509_certificate(path.read_bytes())

    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, config.pki.org_name),
            x509.NameAttribute(NameOID.COMMON_NAME, f"{config.pki.org_name} SA Provisioning"),
        ]))
        .issuer_name(ca._mo_sub_ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=config.pki.cert_validity_days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=False,
                content_commitment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .sign(ca._mo_sub_ca_key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    ))
    os.chmod(key_path, 0o600)
    logger.info("SA provisioning certificate and key created")
    return cert


def new_emaid() -> str:
    """ISO 15118-2 eMAID, 14 characters, no separators."""
    country = os.getenv("OCPI_COUNTRY_CODE", "GB").upper()[:2].ljust(2, "X")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    instance = "".join(secrets.choice(alphabet) for _ in range(9))
    return f"{country}PRS{instance}"


def encrypt_contract_key(oem_cert_der: bytes, contract_key) -> tuple[bytes, bytes]:
    """Encrypt the contract private key to the OEM certificate.

    ISO 15118-2 V2G2-818: ECDH on secp256r1, ConcatKDF, AES-128-CBC.
    The return is the ephemeral public key and the ciphertext with the
    initialisation vector in the first 16 bytes.
    """
    oem_cert = x509.load_der_x509_certificate(oem_cert_der)
    oem_public = oem_cert.public_key()
    if not isinstance(oem_public, ec.EllipticCurvePublicKey):
        raise ValueError("OEM provisioning certificate is not an EC key")

    ephemeral = ec.generate_private_key(ec.SECP256R1())
    shared = ephemeral.exchange(ec.ECDH(), oem_public)
    other_info = bytes([0x01, 0x55, 0x56])
    symmetric = ConcatKDFHash(
        algorithm=hashes.SHA256(),
        length=16,
        otherinfo=other_info,
    ).derive(shared)
    iv = secrets.token_bytes(16)
    private_value = contract_key.private_numbers().private_value.to_bytes(32, "big")
    encryptor = Cipher(algorithms.AES(symmetric), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(private_value) + encryptor.finalize()
    dh_public = ephemeral.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    return dh_public, iv + ciphertext


def decrypt_contract_key(encrypted_with_iv: bytes, oem_private, dh_public: bytes) -> bytes:
    """Reverse of encrypt_contract_key. Used to prove a response can be opened."""
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), dh_public)
    shared = oem_private.exchange(ec.ECDH(), peer)
    symmetric = ConcatKDFHash(
        algorithm=hashes.SHA256(), length=16, otherinfo=bytes([0x01, 0x55, 0x56])
    ).derive(shared)
    iv, ciphertext = encrypted_with_iv[:16], encrypted_with_iv[16:]
    decryptor = Cipher(algorithms.AES(symmetric), modes.CBC(iv)).decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def _chain(leaf: bytes, sub: bytes | None = None) -> dict:
    from expy.v2gjson import iso2

    sub_certs = iso2.SubCertificatesType([bytearray(sub)]) if sub is not None else None
    return iso2.CertificateChainType(bytearray(leaf), SubCertificates=sub_certs)


def _installation_body(emaid: str, contract_der: bytes, dh_public: bytes, encrypted: bytes, response_code: int) -> dict:
    from expy.v2gjson import iso2

    provisioning = ensure_sa_provisioning_cert()
    code = iso2.responseCodeType(response_code)
    return {
        "CertificateInstallationRes": iso2.CertificateInstallationResType(
            code,
            _chain(_der(provisioning), _der(ca._mo_sub_ca_cert)),
            _chain(contract_der, _der(ca._mo_sub_ca_cert)),
            iso2.ContractSignatureEncryptedPrivateKeyType("ID1", bytearray(encrypted)),
            iso2.DiffieHellmanPublickeyType("ID2", bytearray(dh_public)),
            iso2.EMAIDType("ID3", emaid),
        )
    }


def _update_body(emaid: str, contract_der: bytes, dh_public: bytes, encrypted: bytes) -> dict:
    body = _installation_body(emaid, contract_der, dh_public, encrypted, RESPONSE_OK)
    res = body.pop("CertificateInstallationRes")
    body["CertificateUpdateRes"] = res
    return body


def _session_id(decoded: dict) -> bytes:
    session = find_key(decoded, "SessionID")
    raw = field_bytes(session) if session is not None else None
    if raw:
        return raw[:8].ljust(8, b"\x00")
    return b"\x00" * 8


def _emaid_from_request(decoded: dict) -> str | None:
    emaid = find_key(decoded, "eMAID")
    if isinstance(emaid, dict) and "CONTENT" in emaid:
        return field_text(emaid["CONTENT"])
    return field_text(emaid)


async def _issue_contract(emaid: str):
    """Create a contract key and have the MO sub-CA sign it."""
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, config.pki.org_name),
            x509.NameAttribute(NameOID.COMMON_NAME, emaid),
        ]))
        .sign(key, hashes.SHA256())
    )
    cert_pem, _serial = await ca.sign_contract_cert(
        csr.public_bytes(Encoding.PEM).decode(), emaid
    )
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    return key, cert


async def _remember_emaid(emaid: str) -> None:
    from state.postgres import db

    async with db.write() as conn:
        await conn.execute(
            """
            INSERT INTO ocpp.authorization_cache (token, type, status, display_name)
            VALUES ($1, 'eMAID', 'Accepted', $1)
            ON CONFLICT (token) DO UPDATE
                SET type = 'eMAID', status = 'Accepted', display_name = $1
            """,
            emaid,
        )


async def handle_ev_certificate(payload: dict, charge_point_id: str) -> dict:
    """Build the Get15118EVCertificate response."""
    action = payload.get("action", "Install")
    exi_request = payload.get("exiRequest") or ""
    if not exi_request:
        return {"status": "Failed", "exiResponse": ""}

    try:
        decoded, kind = decode_request(exi_request)
    except Exception as exc:
        logger.warning("[%s] Get15118EVCertificate EXI decode failed: %s", charge_point_id, exc)
        return {"status": "Failed", "exiResponse": ""}

    try:
        if action == "Update":
            emaid = _emaid_from_request(decoded) or new_emaid()
        else:
            emaid = new_emaid()
        oem_der = field_bytes(find_key(decoded, "OEMProvisioningCert"))
        if not oem_der:
            raise ValueError("OEM provisioning certificate missing")
        contract_key, contract_cert = await _issue_contract(emaid)
        dh_public, encrypted = encrypt_contract_key(oem_der, contract_key)
        if action == "Update":
            body = _update_body(emaid, _der(contract_cert), dh_public, encrypted)
        else:
            body = _installation_body(
                emaid, _der(contract_cert), dh_public, encrypted, RESPONSE_OK
            )
        exi_response = encode_response(body, "document", _session_id(decoded))
        await _remember_emaid(emaid)
        logger.info(
            "[%s] Contract certificate issued for %s action=%s request=%s",
            charge_point_id, emaid, action, kind,
        )
        return {"status": "Accepted", "exiResponse": exi_response}
    except Exception as exc:
        logger.exception("[%s] Get15118EVCertificate failed: %s", charge_point_id, exc)
        return {"status": "Failed", "exiResponse": ""}


def _normalise_hash(value: str) -> str:
    text = (value or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    return text


def _hash_algo(name: str):
    return _HASHES.get((name or "SHA256").upper(), hashes.SHA256())


async def _load_contract(serial_hex: str | None, emaid: str):
    from state.postgres import db

    async with db.read() as conn:
        if serial_hex:
            row = await conn.fetchrow(
                """
                SELECT serial, subject, status, pem, not_before, not_after
                FROM ocpp.pki_certificates
                WHERE type = 'contract'
                  AND lower(ltrim(serial, '0')) = lower(ltrim($1, '0'))
                """,
                serial_hex.lower().lstrip("0") or "0",
            )
            if row:
                return row
        return await conn.fetchrow(
            """
            SELECT serial, subject, status, pem, not_before, not_after
            FROM ocpp.pki_certificates
            WHERE type = 'contract' AND subject = $1
            ORDER BY issued_at DESC
            LIMIT 1
            """,
            emaid,
        )


def _hashes_match(cert: x509.Certificate, issuer: x509.Certificate, item: dict) -> bool:
    algo = _hash_algo(item.get("hashAlgorithm", "SHA256"))
    try:
        request = ocsp.OCSPRequestBuilder().add_certificate(cert, issuer, algo).build()
    except Exception:
        return False
    serial_text = _normalise_hash(str(item.get("serialNumber", "")))
    try:
        presented_serial = int(serial_text, 16) if serial_text else cert.serial_number
    except ValueError:
        try:
            presented_serial = int(serial_text)
        except ValueError:
            return False
    if presented_serial != cert.serial_number:
        return False
    name_hash = _normalise_hash(str(item.get("issuerNameHash", "")))
    key_hash = _normalise_hash(str(item.get("issuerKeyHash", "")))
    if name_hash and name_hash != request.issuer_name_hash.hex():
        return False
    if key_hash and key_hash != request.issuer_key_hash.hex():
        return False
    return True


async def authorize_contract(emaid: str, cert_hash_data: list | None) -> str:
    """Accept a vehicle only when its contract certificate is valid."""
    from state.postgres import db

    async with db.read() as conn:
        cached = await conn.fetchrow(
            "SELECT status FROM ocpp.authorization_cache WHERE token = $1", emaid
        )
    if cached and cached["status"] in {
        AuthorizationStatus.BLOCKED,
        AuthorizationStatus.EXPIRED,
    }:
        return cached["status"]

    serial_hex = None
    hash_item = None
    if cert_hash_data:
        hash_item = cert_hash_data[0] if isinstance(cert_hash_data[0], dict) else None
        if hash_item:
            serial_hex = _normalise_hash(str(hash_item.get("serialNumber", "")))

    row = await _load_contract(serial_hex, emaid)
    if not row:
        return cached["status"] if cached else AuthorizationStatus.INVALID

    if row["status"] == "revoked":
        return AuthorizationStatus.BLOCKED

    now = datetime.now(timezone.utc)
    if row["not_after"] and row["not_after"] < now:
        return AuthorizationStatus.EXPIRED
    if row["not_before"] and row["not_before"] > now:
        return AuthorizationStatus.INVALID

    try:
        cert = x509.load_pem_x509_certificate(row["pem"].encode())
    except Exception:
        return AuthorizationStatus.INVALID

    if cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME):
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        if emaid and cn != emaid:
            return AuthorizationStatus.INVALID

    chain = await ca.validate_cert_chain(row["pem"])
    if not chain.get("valid"):
        return AuthorizationStatus.INVALID

    if hash_item and not _hashes_match(cert, ca._mo_sub_ca_cert, hash_item):
        return AuthorizationStatus.INVALID

    return AuthorizationStatus.ACCEPTED
