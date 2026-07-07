import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timezone


def new_id() -> str:
    return uuid.uuid4().hex


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- UI gate: a stateless cookie, hmac-signed with the password itself. It only exists
# to keep unknown browsers out; changing the password invalidates every session. ---
def _session_sig(password: str, msg: str = "postbox-ui") -> str:
    return hmac.new(password.encode(), msg.encode(), hashlib.sha256).hexdigest()


def make_session_cookie(password: str) -> str:
    return _session_sig(password)


def valid_session_cookie(password: str, cookie: str | None) -> bool:
    if not password or not cookie:
        return False
    return hmac.compare_digest(cookie, _session_sig(password))

