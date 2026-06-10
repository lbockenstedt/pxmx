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

    def sign(self, msg: Dict[str, Any]) -> str:
        """Signs a message by creating an HMAC-SHA256 hash of its JSON representation."""
        # Exclude signature from the data being signed
        data = {k: v for k, v in msg.items() if k != "signature"}
        message_bytes = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
        return hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()

    def verify(self, msg: Dict[str, Any]) -> bool:
        """Verifies the signature of a message."""
        sig = msg.get("signature")
        if not sig:
            return False

        expected = self.sign(msg)
        return hmac.compare_digest(expected, sig)
