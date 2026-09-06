"""HMAC JWT хаба motok. Формат совпадает с motok.person.issue_homework_jwt / verify_homework_jwt."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode

HOMEWORK_MODULE = "homework"


class HubJwtError(Exception):
    """Token rejected."""


def _b64url(raw: bytes) -> str:
    return urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def issue_homework_jwt(person_id: str, *, secret: str, ttl_s: int = 300) -> str:
    now = int(time.time())
    payload = {
        "sub": person_id,
        "modules": [HOMEWORK_MODULE],
        "iat": now,
        "exp": now + ttl_s,
    }
    header = {"alg": "HS256", "typ": "JWT"}
    body = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url(sig)}"


def verify_homework_jwt(token: str, *, secret: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise HubJwtError("jwt shape")
    body = f"{parts[0]}.{parts[1]}"
    expected = hmac.new(
        secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).digest()
    pad = "=" * (-len(parts[2]) % 4)
    given = urlsafe_b64decode(parts[2] + pad)
    if not hmac.compare_digest(expected, given):
        raise HubJwtError("jwt signature")
    pad_p = "=" * (-len(parts[1]) % 4)
    payload = json.loads(urlsafe_b64decode(parts[1] + pad_p))
    if int(payload.get("exp", 0)) <= int(time.time()):
        raise HubJwtError("jwt expired")
    if HOMEWORK_MODULE not in payload.get("modules", []):
        raise HubJwtError("jwt module")
    if not payload.get("sub"):
        raise HubJwtError("jwt sub")
    return payload
