"""HMAC-SHA256 message signing for the pxmx agent.

``MessageSigner`` canonicalizes a message (recursively sorted keys →
deterministic JSON) and signs/verifies it with HMAC-SHA256. Mirrors the Hub-side
signer at ``lm/core/src/security/signer.py`` so the agent and Hub agree on the
canonical form. Audience: pxmx developers.
"""

# hmac computes and compares message signatures (compare_digest is timing-safe).
import hmac
# hashlib supplies the SHA-256 digest used inside HMAC.
import hashlib
# json produces the compact canonical serialization that gets signed.
import json
# Type hints only; they document the shapes of values passed around.
from typing import Dict, Any

class MessageSigner:
    """Utility for signing and verifying messages using HMAC-SHA256.
    Ensures deterministic serialization to prevent signature mismatches.
    """

    def __init__(self, secret: str):
        """Store the shared secret used for every sign/verify call."""
        # Shared secret agreed with the hub; never sent over the wire.
        self.secret = secret

    def _canonicalize(self, obj: Any) -> Any:
        """Recursively sorts dictionary keys to ensure deterministic serialization."""
        # Dicts: rebuild with keys in sorted order, canonicalizing each value too.
        if isinstance(obj, dict):
            return {k: self._canonicalize(obj[k]) for k in sorted(obj.keys())}
        # Lists keep their order, but their elements may contain dicts that need sorting.
        elif isinstance(obj, list):
            return [self._canonicalize(i) for i in obj]
        # Scalars (str, int, bool, None) are already deterministic.
        return obj

    def sign(self, msg: Dict[str, Any]) -> str:
        """Signs a message by creating an HMAC-SHA256 hash of its canonical JSON representation."""
        # Exclude signature from the data being signed
        data = {k: v for k, v in msg.items() if k != "signature"}
        # Sort keys at every level so both sides produce identical JSON.
        canonical_data = self._canonicalize(data)
        # Compact separators (no spaces) must match the hub's serialization byte-for-byte.
        message_bytes = json.dumps(canonical_data, separators=(',', ':')).encode()
        # Hex digest of HMAC-SHA256 keyed by the shared secret.
        return hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()

    def verify(self, msg: Dict[str, Any]) -> bool:
        """Verifies the signature of a message."""
        # Pull out the signature the sender attached.
        sig = msg.get("signature")
        # Unsigned messages are never trusted.
        if not sig:
            return False

        # Recompute what the signature should be and compare in constant time (prevents timing
        # attacks).
        expected = self.sign(msg)
        return hmac.compare_digest(expected, sig)

    def sign_bytes(self, message_bytes: bytes) -> str:
        """HMAC-SHA256 over raw bytes (for the <sig>.<body> wire format)."""
        # Same HMAC as sign(), but over bytes exactly as received/sent (no re-serialization).
        return hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()

    def verify_bytes(self, message_bytes: bytes, signature: str) -> bool:
        """Check a raw-bytes signature in constant time; True when it matches."""
        # compare_digest avoids leaking how many leading characters matched.
        return hmac.compare_digest(self.sign_bytes(message_bytes), signature)


def encode_frame(signer, msg: Dict[str, Any]) -> str:
    """Wire form ``<sig>.<body>`` — body serialized ONCE, sig over those exact
    bytes. Byte-identical to lm/core/src/security/signer.py so the Hub verifies
    the RECEIVED bytes directly (no re-serialization). Unsigned = ''.<body>."""
    # Serialize once; the signature below covers exactly these bytes.
    body = json.dumps(msg, separators=(',', ':'))
    # Sign the body when a signer is configured, otherwise leave the signature empty.
    sig = signer.sign_bytes(body.encode()) if signer is not None else ""
    # Join as <sig>.<body>; JSON never starts with '.', so the first dot is the separator.
    return sig + "." + body


def split_frame(wire: str):
    """Split ``<sig>.<body>`` → (sig, body) on the FIRST '.'; unsigned = ('', body)."""
    # Split on the first '.' only, since the JSON body may itself contain dots.
    sig, sep, body = wire.partition(".")
    # No dot at all means a bare body with no signature.
    return ("", wire) if not sep else (sig, body)
