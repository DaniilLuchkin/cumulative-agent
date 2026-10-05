"""Telegram's official bot-token HMAC verification; no trust in initDataUnsafe."""
import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl


class InvalidTelegramData(ValueError):
    pass


def verify_init_data(raw: str, bot_token: str, max_age: int, *, clock: float | None = None) -> int:
    if not bot_token or not raw or len(raw.encode()) > 16384:
        raise InvalidTelegramData("Invalid Telegram authentication")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=100)
        fields = dict(pairs)
        if len(fields) != len(pairs) or any(not re.fullmatch(r"[a-z_]{1,64}", key) for key in fields):
            raise ValueError("duplicate fields")
        received_hash = fields.pop("hash")
        if not re.fullmatch(r"[0-9a-f]{64}", received_hash):
            raise ValueError("invalid hash")
        data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(received_hash, expected):
            raise ValueError("invalid signature")
        current = time.time() if clock is None else clock
        issued = int(fields["auth_date"])
        if issued > current + 30 or current - issued > max_age:
            raise ValueError("expired or future data")
        user = json.loads(fields["user"])
        owner = user["id"]
        if type(owner) is not int or owner <= 0:
            raise ValueError("invalid user")
        return owner
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise InvalidTelegramData("Invalid or expired Telegram authentication") from None
