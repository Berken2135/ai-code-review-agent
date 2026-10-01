import hashlib
import hmac


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Check GitHub's `X-Hub-Signature-256` header against the raw request body."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # Compare as bytes: compare_digest raises TypeError on non-ASCII str input.
    return hmac.compare_digest(expected.encode(), signature_header.encode())
