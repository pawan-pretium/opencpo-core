"""Schema-informed EXI for ISO 15118-2 certificate messages.

The codec is libcbv2g through the EXPy binding. A charger sends the
CertificateInstallationReq as EXI. The reply must be EXI of the same kind.
"""
import base64
import logging

logger = logging.getLogger(__name__)

_processor = None


def processor():
    """Load the ISO 15118-2 processor once."""
    global _processor
    if _processor is None:
        from expy import EXIProcessor, Namespace

        _processor = EXIProcessor(Namespace.ISO2)
    return _processor


def bfield(data: bytes) -> dict:
    """Byte field in the shape libcbv2g expects."""
    return {"bytes": list(data), "bytesLen": len(data)}


def cfield(text: str) -> dict:
    """Character field in the shape libcbv2g expects."""
    raw = list(text.encode("utf-8"))
    return {"characters": raw, "charactersLen": len(raw)}


def field_bytes(value) -> bytes | None:
    """Read a libcbv2g byte field."""
    if isinstance(value, dict) and "bytes" in value:
        return bytes(value["bytes"][: value.get("bytesLen", len(value["bytes"]))])
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return None


def field_text(value) -> str | None:
    """Read a libcbv2g character field."""
    if isinstance(value, dict) and "characters" in value:
        chars = value["characters"]
        if isinstance(chars, str):
            return chars
        length = value.get("charactersLen", len(chars))
        return bytes(chars[:length]).decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return None


def find_key(node, key: str):
    """Depth-first search for a dictionary key."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for child in node.values():
            found = find_key(child, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for child in node:
            found = find_key(child, key)
            if found is not None:
                return found
    return None


def decode_request(exi_b64: str) -> tuple[dict, str]:
    """Decode a charger EXI request.

    Returns the decoded object and the encoding used: "fragment" or "document".
    OCPP carries the certificate message either as an EXI fragment or as a
    full V2G document. The reply uses the same encoding.
    """
    raw = base64.b64decode(exi_b64)
    codec = processor()
    errors = []
    for kind, method in (
        ("fragment", "decode_fragment"),
        ("document", "decode"),
    ):
        try:
            decoded = getattr(codec, method)(raw)
            return decoded, kind
        except Exception as exc:
            errors.append(f"{kind}: {exc}")
    raise ValueError("EXI request could not be decoded (" + "; ".join(errors) + ")")


def encode_response(body: dict, kind: str, session_id: bytes) -> str:
    """Encode a certificate response and return base64."""
    codec = processor()
    if kind == "document":
        document = {
            "Header": {"SessionID": bfield(session_id[:8].ljust(8, b"\x00"))},
            "Body": body,
        }
        encoded = codec.encode(document)
    else:
        encoded = codec.encode_fragment(body)
    return base64.b64encode(encoded).decode("ascii")
