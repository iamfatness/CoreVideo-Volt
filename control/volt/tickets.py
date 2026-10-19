"""HMAC media tickets. The token is `exp.hexhmac` of `asset|kind|exp`; the path binds the asset."""

from __future__ import annotations

import hashlib
import hmac


class TicketError(Exception):
    def __init__(self, code: str, reason: str = ""):
        super().__init__(reason or code)
        self.code = code


KINDS = ("proxy", "thumb")


def issue(secret: str, asset_id: str, kind: str, ttl: int, now: int) -> tuple[str, int]:
    if kind not in KINDS:
        raise TicketError("ticket_invalid", "kind must be proxy or thumb")
    exp = int(now) + int(ttl)
    return _token(secret, asset_id, kind, exp), exp


def verify(secret: str, asset_id: str, kind: str, token: str, now: int) -> str:
    if kind not in KINDS or not isinstance(token, str) or "." not in token:
        raise TicketError("ticket_invalid", "ticket is not valid")
    exp_s, _, _digest = token.partition(".")
    try:
        exp = int(exp_s)
    except ValueError:
        raise TicketError("ticket_invalid", "ticket is not valid") from None
    expected = _token(secret, asset_id, kind, exp)
    if not hmac.compare_digest(expected, token):
        raise TicketError("ticket_invalid", "ticket is not valid")
    if int(now) >= exp:
        raise TicketError("ticket_expired", "ticket has expired")
    return asset_id


def _token(secret: str, asset_id: str, kind: str, exp: int) -> str:
    msg = f"{asset_id}|{kind}|{exp}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"{exp}.{digest}"
