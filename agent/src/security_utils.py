"""HMAC-SHA256 message signing for the pxmx agent.

``MessageSigner`` canonicalizes a message (recursively sorted keys →
deterministic JSON) and signs/verifies it with HMAC-SHA256. Mirrors the Hub-side
signer at ``lm/core/src/security/signer.py`` so the agent and Hub agree on the
canonical form. Audience: pxmx developers.
"""

import hmac
import hashlib
import json
from typing import Dict, Any

class MessageSigner:
    """Utility for signing and verifying messages using HMAC-SHA256.
    Ensures deterministic serialization to prevent signature mismatches.
    """

    def __init__(self, secret: str):
        self.secret = secret

    def _canonicalize(self, obj: Any) -> Any:
        """Recursively sorts dictionary keys to ensure deterministic serialization."""
        if isinstance(obj, dict):
            return {k: self._canonicalize(obj[k]) for k in sorted(obj.keys())}
        elif isinstance(obj, list):
            return [self._canonicalize(i) for i in obj]
        return obj

    def sign(self, msg: Dict[str, Any]) -> str:
        """Signs a message by creating an HMAC-SHA256 hash of its canonical JSON representation."""
        # Exclude signature from the data being signed
        data = {k: v for k, v in msg.items() if k != "signature"}
        canonical_data = self._canonicalize(data)
        message_bytes = json.dumps(canonical_data, separators=(',', ':')).encode()
        return hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()

    def verify(self, msg: Dict[str, Any]) -> bool:
        """Verifies the signature of a message."""
        sig = msg.get("signature")
        if not sig:
            return False

        expected = self.sign(msg)
        return hmac.compare_digest(expected, sig)

    def sign_bytes(self, message_bytes: bytes) -> str:
        """HMAC-SHA256 over raw bytes (for the <sig>.<body> wire format)."""
        return hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()

    def verify_bytes(self, message_bytes: bytes, signature: str) -> bool:
        return hmac.compare_digest(self.sign_bytes(message_bytes), signature)


def encode_frame(signer, msg: Dict[str, Any]) -> str:
    """Wire form ``<sig>.<body>`` — body serialized ONCE, sig over those exact
    bytes. Byte-identical to lm/core/src/security/signer.py so the Hub verifies
    the RECEIVED bytes directly (no re-serialization). Unsigned = ''.<body>."""
    body = json.dumps(msg, separators=(',', ':'))
    sig = signer.sign_bytes(body.encode()) if signer is not None else ""
    return sig + "." + body


def split_frame(wire: str):
    """Split ``<sig>.<body>`` → (sig, body) on the FIRST '.'; unsigned = ('', body)."""
    sig, sep, body = wire.partition(".")
    return ("", wire) if not sep else (sig, body)
