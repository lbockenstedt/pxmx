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
