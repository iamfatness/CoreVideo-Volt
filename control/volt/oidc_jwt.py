"""OpenID Connect ID-token verification: RS256 in pure standard-library Python.

Only verification exists here; Volt never signs. Pure functions plus a small
JWKS cache whose clock and HTTP fetch are injected, so nothing in this module
touches the network or the wall clock by itself.

Hard rules (each has a test):
- the algorithm is exactly "RS256", decided by this module, never by the token;
- the expected PKCS#1 v1.5 block is built from the SHA-256 digest and compared
  whole, in constant time. The padding is never parsed, which is what lets
  Bleichenbacher-style forgeries through lenient verifiers;
- the modulus is at least 2048 bits and odd, the exponent is odd and at least
  65537, the signature is exactly as long as the modulus and below it;
- claims are checked only after the signature.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
import threading

MIN_MODULUS_BITS = 2048
MIN_EXPONENT = 65537
MAX_EXPONENT_BITS = 64
MAX_TOKEN_BYTES = 16384
STALE_WINDOW = 86400  # cached keys keep verifying this long past expiry when the provider is down

# DER prefix of DigestInfo for SHA-256 (RFC 8017 section 9.2, note 1).
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_B64URL = re.compile(r"\A[A-Za-z0-9_-]*\Z")


class TokenError(Exception):
    """Verification refused. `code` is one of malformed, alg_unsupported,
    unknown_kid, bad_signature, bad_key, unavailable, or bad_claim:<name>."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# -- encoding helpers ------------------------------------------------

def _b64d(text):
    if not isinstance(text, str) or not _B64URL.match(text) or len(text) % 4 == 1:
        raise TokenError("malformed")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise TokenError("malformed")


def _no_constant(_name):
    raise ValueError("non-finite number")


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _json_object(raw):
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates,
                           parse_constant=_no_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise TokenError("malformed")
    if not isinstance(value, dict):
        raise TokenError("malformed")
    return value


# -- the RSA operation -----------------------------------------------

def rsa_verify_rs256(n, e, signing_input, signature):
    """True when `signature` is a valid RSASSA-PKCS1-v1_5 / SHA-256 signature.

    Public-key operation only. The expected encoded message is built here from
    the digest and compared whole with hmac.compare_digest."""
    if not (isinstance(n, int) and isinstance(e, int) and n > 0 and e > 1):
        return False
    if not isinstance(signature, (bytes, bytearray)):
        return False
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    try:
        block = pow(s, e, n).to_bytes(k, "big")
    except OverflowError:
        return False
    tail = _SHA256_PREFIX + hashlib.sha256(signing_input).digest()
    if k < len(tail) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - 3 - len(tail)) + b"\x00" + tail
    return hmac.compare_digest(block, expected)


# -- keys ------------------------------------------------------------

def _key_from_jwk(jwk):
    """(n, e) from a JWK dict, or TokenError('bad_key')."""
    if jwk.get("kty") != "RSA":
        raise TokenError("bad_key")
    if "use" in jwk and jwk["use"] != "sig":
        raise TokenError("bad_key")
    if "alg" in jwk and jwk["alg"] != "RS256":
        raise TokenError("bad_key")
    try:
        n = int.from_bytes(_b64d(jwk.get("n")), "big")
        e = int.from_bytes(_b64d(jwk.get("e")), "big")
    except TokenError:
        raise TokenError("bad_key")
    if n.bit_length() < MIN_MODULUS_BITS or n % 2 == 0:
        raise TokenError("bad_key")
    if e < MIN_EXPONENT or e % 2 == 0 or e.bit_length() > MAX_EXPONENT_BITS:
        raise TokenError("bad_key")
    return n, e


def _find_jwk(jwks, kid):
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if not isinstance(keys, list):
        raise TokenError("unknown_kid")
    found = [k for k in keys if isinstance(k, dict) and k.get("kid") == kid]
    if not found:
        raise TokenError("unknown_kid")
    if len(found) > 1:
        raise TokenError("bad_key")  # ambiguous: refuse rather than pick one
    return found[0]


# -- claims ----------------------------------------------------------

def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _claim_time(claims, name, required=True):
    if name not in claims:
        if required:
            raise TokenError("bad_claim:" + name)
        return None
    if not _is_number(claims[name]):
        raise TokenError("bad_claim:" + name)
    return claims[name]


def _check_claims(claims, issuer, audience, nonce, now, leeway, max_age):
    if not isinstance(claims.get("iss"), str) or not hmac.compare_digest(
            claims["iss"].encode("utf-8"), issuer.encode("utf-8")):
        raise TokenError("bad_claim:iss")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise TokenError("bad_claim:sub")
    aud = claims.get("aud")
    if isinstance(aud, str):
        if aud != audience:
            raise TokenError("bad_claim:aud")
    elif isinstance(aud, list) and all(isinstance(a, str) for a in aud):
        if audience not in aud:
            raise TokenError("bad_claim:aud")
        if claims.get("azp") != audience:
            raise TokenError("bad_claim:azp")
    else:
        raise TokenError("bad_claim:aud")
    exp = _claim_time(claims, "exp")
    if exp + leeway <= now:
        raise TokenError("bad_claim:exp")
    iat = _claim_time(claims, "iat")
    if iat > now + leeway or iat < now - max_age:
        raise TokenError("bad_claim:iat")
    nbf = _claim_time(claims, "nbf", required=False)
    if nbf is not None and nbf > now + leeway:
        raise TokenError("bad_claim:nbf")
    got = claims.get("nonce")
    if not isinstance(got, str) or not nonce or not hmac.compare_digest(
            got.encode("utf-8"), nonce.encode("utf-8")):
        raise TokenError("bad_claim:nonce")


def verify_id_token(token, jwks, *, issuer, audience, nonce, now, leeway=120, max_age=600):
    """Verify an RS256 ID token and return its claims, or raise TokenError."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_BYTES:
        raise TokenError("malformed")
    parts = token.split(".")
    if len(parts) != 3 or not parts[0] or not parts[1]:
        raise TokenError("malformed")
    head_b64, payload_b64, sig_b64 = parts
    header = _json_object(_b64d(head_b64))
    if header.get("alg") != "RS256":  # fixed allow-list: none, HS*, ES*, PS*, rs256 all refused
        raise TokenError("alg_unsupported")
    if "crit" in header:
        raise TokenError("malformed")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise TokenError("unknown_kid")
    n, e = _key_from_jwk(_find_jwk(jwks, kid))
    if not sig_b64:
        raise TokenError("malformed")
    signature = _b64d(sig_b64)
    payload_raw = _b64d(payload_b64)
    signing_input = (head_b64 + "." + payload_b64).encode("ascii")
    if not rsa_verify_rs256(n, e, signing_input, signature):
        raise TokenError("bad_signature")
    claims = _json_object(payload_raw)
    _check_claims(claims, issuer, audience, nonce, now, leeway, max_age)
    return claims


# -- JWKS cache ------------------------------------------------------

class JwksCache:
    """Per-provider JWKS cache with an injected clock and fetch.

    fetch(provider) -> (jwks_dict, max_age_seconds_or_None); it may raise.
    now() -> seconds.  Entry lifetime is max_age clamped to [floor, ceiling].
    An unknown kid triggers at most one refetch per `refetch_gap` seconds per
    provider, so forged kids cannot make Volt hammer the provider. When the
    provider cannot be reached, known keys keep verifying for STALE_WINDOW
    past expiry; an unknown kid then fails with TokenError('unavailable')."""

    def __init__(self, fetch, now, floor=300, ceiling=86400, refetch_gap=60):
        self._fetch = fetch
        self._now = now
        self.floor = floor
        self.ceiling = ceiling
        self.refetch_gap = refetch_gap
        self._entries = {}
        self._lock = threading.Lock()

    @staticmethod
    def _has_kid(jwks, kid):
        return any(isinstance(k, dict) and k.get("kid") == kid for k in jwks["keys"])

    def _refresh(self, provider, entry, now, miss=False):
        """Fetch if allowed. Failures back off for refetch_gap; an unknown kid
        may trigger one refetch per refetch_gap."""
        miss_at = None
        if entry is not None:
            if not entry["ok"] and now - entry["attempt"] < self.refetch_gap:
                return
            if miss:
                if now - entry["miss_at"] < self.refetch_gap:
                    return
                miss_at = now
            else:
                miss_at = entry["miss_at"]
        try:
            jwks, max_age = self._fetch(provider)
            if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
                raise ValueError("not a JWKS")
        except Exception:
            if entry is not None:
                entry.update(ok=False, attempt=now, miss_at=miss_at)
            else:
                self._entries[provider] = {"jwks": None, "expires": 0, "attempt": now,
                                           "ok": False, "miss_at": float("-inf")}
            return
        ttl = max_age if _is_number(max_age) else self.floor
        ttl = min(max(ttl, self.floor), self.ceiling)
        self._entries[provider] = {
            "jwks": jwks, "expires": now + ttl, "attempt": now, "ok": True,
            "miss_at": float("-inf") if miss_at is None else miss_at}

    def keys(self, provider, kid):
        """The JWKS to verify a token with this kid against."""
        with self._lock:
            now = self._now()
            entry = self._entries.get(provider)
            if entry is None or entry["jwks"] is None or now >= entry["expires"]:
                self._refresh(provider, entry, now)
            elif not self._has_kid(entry["jwks"], kid):
                self._refresh(provider, entry, now, miss=True)
            entry = self._entries[provider]
            jwks = entry["jwks"]
            if jwks is None:
                raise TokenError("unavailable")
            if self._has_kid(jwks, kid):
                if now < entry["expires"] + STALE_WINDOW:
                    return jwks
                raise TokenError("unavailable")
            if entry["ok"] and now < entry["expires"]:
                raise TokenError("unknown_kid")
            raise TokenError("unavailable")
