"""OpenID Connect sign-in: the authorization-code flow with PKCE, and the mapping from a verified ID token to a Volt user and groups
(docs/superpowers/specs/2026-11-01-collaboration-identity-design.md).

UNVALIDATED AGAINST A REAL PROVIDER. Everything here is tested against tests/oidc_fake.py behind the HTTP seam; what Google and Microsoft
send (claim names, `hd`, `tid`, `email_verified`, the group overage marker) is from published behaviour as recalled, not from a run.

What this module guarantees, in the order a login meets it:

  * `begin` stores one row per flow (state, nonce, PKCE verifier, the chosen provider, a validated `next`) and binds it to the browser with a
    random cookie whose hash is stored. `redirect_uri` comes from VOLT_PUBLIC_URL, never from a request header. The table is capped and pruned.
  * `complete` deletes the flow row first (a replayed state finds nothing, and the delete's row count decides a race), then checks expiry
    and the browser binder, then exchanges the code, then verifies the ID token (volt.oidc_jwt: fixed RS256, iss, aud, nonce, iat, exp),
    then maps it to a user. Every refusal writes a `login_failed` audit row (rate limited, with a count) and creates no session.
  * Identity is the pair (issuer, sub), never the email. An email links an unknown identity to an existing, enabled, not-yet-linked user
    only where the provider's address can be trusted (Google: email_verified and the allowed domain; Microsoft: an allowed tenant). No
    silent account takeover: a new `sub` never relinks an already linked user, a changed email never moves a known identity.
  * Provider tokens are used once and dropped. The client secret is read from the environment at load, is not in repr, logs or the audit.

The HTTP seam is the module-level `HTTP` (get_json(url), post_form(url, data)); tests replace it. No test touches the network.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from volt import access, oidc_jwt, sessions, users

log = logging.getLogger("volt.oidc")

FLOW_COOKIE = "volt_flow"
FLOW_TTL = 600                # a login flow lives 10 minutes
MAX_FLOWS = 1000              # live flows at most, so unauthenticated requests cannot grow the table without bound
TIMEOUT = 10                  # seconds for every call to a provider
MAX_BODY = 1 << 20            # a provider answer larger than this is refused
DISCOVERY_TTL = 24 * 3600
DISCOVERY_BACKOFF = 30        # after a failed discovery, no new outbound call for this long
MAX_NEXT = 512
MAX_GROUP_VALUES = 500
PROVIDER_IDS = ("google", "microsoft")
_TENANT_ALIASES = {"common", "organizations", "consumers"}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COOKIE_VALUE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_OAUTH_CODE = re.compile(r"^[a-z_]{1,40}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

MESSAGES = {
    "bad_provider": "No such sign-in provider.",
    "flow_invalid": "This sign-in link was already used, has expired, or was started in a different browser. Go back and start again.",
    "provider_error": "The sign-in provider did not complete the sign-in. Go back and try again.",
    "oidc_unavailable": "The sign-in provider cannot be reached right now. Try again in a minute, or use an access token.",
    "oidc_token_invalid": "The sign-in provider's answer could not be verified.",
    "oidc_alg_unsupported": "The sign-in provider's answer used an unsupported signature.",
    "not_provisioned": "This account has no access to Volt. Ask an administrator to add you.",
    "user_disabled": "This account is disabled. Ask an administrator.",
}
STATUS = {"bad_provider": 422, "flow_invalid": 400, "provider_error": 400, "oidc_unavailable": 503, "oidc_token_invalid": 400,
          "oidc_alg_unsupported": 400, "not_provisioned": 403, "user_disabled": 403}


class ConfigError(ValueError):
    """The OIDC configuration is wrong; restarting will not help."""


class HttpError(Exception):
    """A provider call failed (network, status, size, not JSON). The text is for the log and never holds a secret."""


class LoginError(Exception):
    """A login was refused. `code` is one of MESSAGES; `detail` is a short fixed token for the audit row (never provider-supplied text)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail, self.provider = code, detail, None

    @property
    def status(self) -> int:
        return STATUS.get(self.code, 400)

    @property
    def public_message(self) -> str:
        return MESSAGES.get(self.code, "Sign-in could not be completed.")


# ---- configuration ---------------------------------------------------------------

@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    issuer: str
    client_id: str
    client_secret: str = field(repr=False, compare=False)
    scopes: tuple = ("openid", "email", "profile")
    domains: tuple = ()
    tenants: tuple = ()
    group_claim: str = "groups"
    auto_provision: bool = False


@dataclass(frozen=True)
class OidcConfig:
    public_url: str
    providers: tuple

    @property
    def redirect_uri(self) -> str:
        return self.public_url + "/auth/callback"

    def provider(self, provider_id) -> Optional[Provider]:
        for p in self.providers:
            if p.id == provider_id:
                return p
        return None


_ALLOWED_KEYS = {"id", "label", "issuer", "client_id", "client_secret_env", "scopes", "domains", "tenants", "group_claim", "auto_provision"}


def _text_list(value, what: str) -> tuple:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{what} must be a list of non-empty strings")
    return tuple(v.strip().lower() for v in value)


def _is_loopback_host(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


def load_config(path, env=None) -> Optional[OidcConfig]:
    """Read the JSON file VOLT_OIDC_CONFIG names. None (OIDC off) when no path is given; a path that cannot be read or a file that is wrong
    raises ConfigError, so a typo is loud rather than a silently missing sign-in. Secrets are read from the environment variable each
    provider names (`client_secret_env`); the file never holds one."""
    env = os.environ if env is None else env
    if not path:
        return None
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read the OIDC config {path}: {exc.strerror or exc.__class__.__name__}") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise ConfigError(f"the OIDC config {path} is not valid JSON") from None
    rows = data.get("providers") if isinstance(data, dict) and set(data) <= {"providers"} else None
    if not isinstance(rows, list) or not rows:
        raise ConfigError('the OIDC config must be {"providers": [ ... ]} with at least one provider')

    public = str(env.get("VOLT_PUBLIC_URL") or "").strip().rstrip("/")
    parts = urllib.parse.urlsplit(public) if public else None
    if not public or sessions.origin_of(public) is None or parts.query or parts.fragment:
        raise ConfigError("OIDC needs VOLT_PUBLIC_URL, the https address people open Volt at (for example https://news.example.com)")
    if parts.scheme != "https" and not _is_loopback_host(parts.hostname or ""):
        raise ConfigError("VOLT_PUBLIC_URL must be https unless it is a loopback address; providers refuse plain-http redirect URIs elsewhere")

    providers, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ConfigError("each provider must be an object")
        if "client_secret" in row:
            raise ConfigError("never put the client secret in the file; name an environment variable in client_secret_env")
        unknown = sorted(set(row) - _ALLOWED_KEYS)
        if unknown:
            raise ConfigError("unknown provider setting: " + ", ".join(unknown))
        pid = row.get("id")
        if pid not in PROVIDER_IDS:
            raise ConfigError("provider id must be one of " + ", ".join(PROVIDER_IDS))
        if pid in seen:
            raise ConfigError(f"provider {pid} is listed twice")
        seen.add(pid)
        issuer, client_id = row.get("issuer"), row.get("client_id")
        if not isinstance(issuer, str) or not issuer.startswith("https://") or not isinstance(client_id, str) or not client_id:
            raise ConfigError(f"provider {pid}: issuer must be an https URL and client_id must be set")
        if pid == "microsoft":
            segments = [s.lower() for s in urllib.parse.urlsplit(issuer).path.split("/") if s]
            if any(s in _TENANT_ALIASES or s.startswith("{") for s in segments):
                raise ConfigError("microsoft: the issuer must name one tenant; common, organizations and consumers are not supported")
        env_name = row.get("client_secret_env")
        if not isinstance(env_name, str) or not _ENV_NAME.fullmatch(env_name):
            raise ConfigError(f"provider {pid}: client_secret_env must name an environment variable")
        secret = env.get(env_name)
        if not isinstance(secret, str) or not secret:
            raise ConfigError(f"provider {pid}: the environment variable {env_name} is not set")
        scopes = row.get("scopes")
        scopes = ("openid", "email", "profile") if scopes is None else tuple(scopes) if isinstance(scopes, list) and all(isinstance(s, str) for s in scopes) else None
        if scopes is None or "openid" not in scopes or "offline_access" in scopes:
            raise ConfigError(f"provider {pid}: scopes must be a list that includes openid and never offline_access")
        domains, tenants = _text_list(row.get("domains"), "domains"), _text_list(row.get("tenants"), "tenants")
        group_claim = row.get("group_claim", "groups")
        auto = row.get("auto_provision", False)
        label = row.get("label", pid.capitalize())
        if not isinstance(group_claim, str) or not group_claim or not isinstance(auto, bool) or not isinstance(label, str) or not 1 <= len(label) <= 40:
            raise ConfigError(f"provider {pid}: group_claim, auto_provision or label is not valid")
        if pid == "microsoft" and not tenants:
            raise ConfigError("microsoft: list the tenant id(s) people may sign in from in tenants")
        if pid == "google" and auto and not domains:
            raise ConfigError("google: auto_provision needs domains, or any Google account could create a user")
        providers.append(Provider(pid, label, issuer, client_id, secret, scopes, domains, tenants, group_claim, auto))
    return OidcConfig(public, tuple(providers))


# ---- the HTTP seam -----------------------------------------------------------------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A provider endpoint that redirects is refused, not followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _max_age(headers) -> Optional[int]:
    match = re.search(r"max-age=(\d{1,9})", str(headers.get("Cache-Control") or ""), re.I) if headers is not None else None
    return int(match.group(1)) if match else None


class UrllibHttp:
    """The production client: https only, 10 second timeout, no redirects, bounded body. UNVALIDATED against a real provider."""

    def __init__(self):
        self._opener = urllib.request.build_opener(NoRedirect)

    @staticmethod
    def _https(url):
        if not isinstance(url, str) or not url.startswith("https://"):
            raise HttpError("only https URLs are called")

    def _call(self, request, url):
        self._https(url)
        try:
            try:
                resp = self._opener.open(request, timeout=TIMEOUT)
            except urllib.error.HTTPError as err:
                resp = err
            with resp:
                body = resp.read(MAX_BODY + 1)
                status, headers = resp.status if hasattr(resp, "status") else resp.code, resp.headers
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise HttpError(f"request failed: {exc.__class__.__name__}") from None
        if len(body) > MAX_BODY:
            raise HttpError("response too large")
        return status, headers, body

    @staticmethod
    def _json(body):
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HttpError("response is not JSON") from None

    def get_json_ex(self, url):
        self._https(url)
        status, headers, body = self._call(urllib.request.Request(url, headers={"Accept": "application/json"}), url)
        if status != 200:
            raise HttpError(f"status {status}")
        return self._json(body), _max_age(headers)

    def get_json(self, url):
        return self.get_json_ex(url)[0]

    def post_form(self, url, data):
        self._https(url)
        request = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode("ascii"), method="POST",
                                         headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
        status, _, body = self._call(request, url)
        if 200 <= status < 300 or status in (400, 401):   # a refused exchange answers with a JSON error object
            return self._json(body)
        raise HttpError(f"status {status}")


HTTP = UrllibHttp()
_ctx = threading.local()      # the `now` and HTTP client of the login being handled, for the JWKS cache's callbacks


def _http(http):
    return http if http is not None else HTTP


# ---- discovery and keys ------------------------------------------------------------

_DISCOVERY: dict = {}
_JWKS: dict = {}
_LOCK = threading.Lock()


def reset_caches() -> None:
    """Forget discovery documents and keys (a test swaps providers; an operator can restart)."""
    with _LOCK:
        _DISCOVERY.clear()
        _JWKS.clear()


def _discover(provider: Provider, http, now: float) -> dict:
    """The provider's authorization, token and JWKS endpoints, cached 24 hours. The document's issuer must equal the configured one
    exactly and every endpoint must be https. A failed fetch backs off so unauthenticated requests cannot drive outbound calls."""
    with _LOCK:
        entry = _DISCOVERY.get(provider.issuer) or {}
        if entry.get("doc") and now < entry["expires"]:
            return entry["doc"]
        if now < entry.get("fail_until", float("-inf")):
            if entry.get("doc"):
                return entry["doc"]
            raise LoginError("oidc_unavailable", "discovery backing off")
    try:
        doc = http.get_json(provider.issuer + "/.well-known/openid-configuration")
        if not isinstance(doc, dict) or doc.get("issuer") != provider.issuer:
            raise HttpError("issuer in the discovery document does not match")
        found = {}
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = doc.get(key)
            if not isinstance(value, str) or not value.startswith("https://"):
                raise HttpError(f"{key} is missing or not https")
            found[key] = value
    except Exception as exc:  # noqa: BLE001 - any failure is "unavailable"; the text names the class only
        log.warning("discovery failed provider=%s reason=%s", provider.id, exc.__class__.__name__ if not isinstance(exc, HttpError) else exc)
        with _LOCK:
            old = _DISCOVERY.get(provider.issuer) or {}
            _DISCOVERY[provider.issuer] = {**old, "fail_until": now + DISCOVERY_BACKOFF}
            if old.get("doc"):                            # stale beats down: the endpoints rarely move
                return old["doc"]
        raise LoginError("oidc_unavailable", "discovery failed") from None
    with _LOCK:
        _DISCOVERY[provider.issuer] = {"doc": found, "expires": now + DISCOVERY_TTL}
    return found


def _jwks_cache(provider: Provider) -> oidc_jwt.JwksCache:
    with _LOCK:
        cache = _JWKS.get(provider.issuer)
        if cache is None:
            def fetch(_key, provider=provider):
                uri = _discover(provider, _ctx.http, _ctx.now)["jwks_uri"]
                getter = getattr(_ctx.http, "get_json_ex", None)
                return getter(uri) if getter is not None else (_ctx.http.get_json(uri), None)
            cache = _JWKS[provider.issuer] = oidc_jwt.JwksCache(fetch, lambda: _ctx.now)
        return cache


# ---- small helpers -----------------------------------------------------------------

def safe_next(value) -> str:
    """The return path after sign-in. Exactly one leading slash, printable ASCII only (no backslash, control character, scheme or
    //host form), at most 512 characters; anything else is the root. Never a full URL, so it cannot leave this site."""
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_NEXT or value[0] != "/" or value.startswith("//") or "\\" in value:
        return "/"
    if any(not 0x20 <= ord(ch) <= 0x7e for ch in value):
        return "/"
    return value


def _token() -> str:
    return secrets.token_urlsafe(32)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def flow_cookie_header(value: str, secure: bool) -> str:
    return f"{FLOW_COOKIE}={value}; Path=/auth; HttpOnly; SameSite=Lax; Max-Age={FLOW_TTL}" + ("; Secure" if secure else "")


def clear_flow_cookie(secure: bool) -> str:
    return f"{FLOW_COOKIE}=; Path=/auth; HttpOnly; SameSite=Lax; Max-Age=0" + ("; Secure" if secure else "")


def flow_cookie_value(header) -> str:
    """The volt_flow value out of a Cookie header, or '' (anything that is not our own random shape is not a candidate)."""
    for piece in (header or "").split(";"):
        name, sep, value = piece.strip().partition("=")
        if sep and name == FLOW_COOKIE:
            return value if _COOKIE_VALUE.fullmatch(value) else ""
    return ""


def _prune_flows(store, now: float) -> int:
    """Delete expired flows and return how many live ones remain."""
    live = 0
    for row in store.conn.execute("SELECT state, expires_at FROM auth_flow").fetchall():
        if now >= sessions._epoch(row["expires_at"]):
            store.conn.execute("DELETE FROM auth_flow WHERE state = ?", (row["state"],))
        else:
            live += 1
    store.conn.commit()
    return live


# ---- begin -------------------------------------------------------------------------

def begin(store, cfg: OidcConfig, provider_id, next_value, now: float, http=None) -> tuple[str, str]:
    """Start a login: returns (the provider's authorization URL, the value of the volt_flow cookie to set on this browser)."""
    provider = cfg.provider(provider_id) if isinstance(provider_id, str) else None
    if provider is None:
        raise LoginError("bad_provider")
    endpoints = _discover(provider, _http(http), now)
    if _prune_flows(store, now) >= MAX_FLOWS:
        log.warning("sign-in flow table is full; refusing new logins until some expire")
        raise LoginError("oidc_unavailable", "too many logins in flight")
    state, nonce, verifier, binder = _token(), _token(), _token(), _token()
    store.conn.execute(
        "INSERT INTO auth_flow (state, nonce, code_verifier, provider, next, binder_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (state, nonce, verifier, provider.id, safe_next(next_value), _sha(binder), sessions._stamp(now), sessions._stamp(now + FLOW_TTL)))
    store.conn.commit()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    params = {"response_type": "code", "client_id": provider.client_id, "redirect_uri": cfg.redirect_uri, "scope": " ".join(provider.scopes),
              "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
    base = endpoints["authorization_endpoint"]
    return base + ("&" if "?" in base else "?") + urllib.parse.urlencode(params), binder


# ---- complete ----------------------------------------------------------------------

@dataclass(frozen=True)
class LoginResult:
    user_name: str
    next: str
    provider: str
    session_cookie: str = field(repr=False)
    csrf: str = field(repr=False)
    groups_overage: bool = False


def _audit_failure(store, err: LoginError) -> None:
    """One login_failed row per second at most, with a count: a hostile client cannot fill the table. Rows hold the provider and a fixed
    reason code, never a state, code, cookie, token or provider text. (The count is written into the second's own row; nothing else of
    an audit row is ever changed.)"""
    try:
        actor = "oidc:" + err.provider if err.provider else "oidc"
        detail = err.code + (":" + err.detail if err.detail else "")
        last = store.conn.execute(
            "SELECT id, at, detail FROM audit WHERE action = 'login_failed' ORDER BY at DESC, id DESC LIMIT 1").fetchone()
        if last is not None and time.time() - sessions._epoch(last["at"]) < 1.0:
            match = re.search(r" x(\d+)$", last["detail"] or "")
            count, base = (int(match.group(1)) + 1, last["detail"][:match.start()]) if match else (2, last["detail"] or "")
            store.conn.execute("UPDATE audit SET detail = ? WHERE id = ?", (f"{base} x{count}", last["id"]))
            store.conn.commit()
            return
        store.append_audit(actor, "login_failed", "session", err.provider or "-", detail)
    except Exception:  # noqa: BLE001 - failing to record a refusal must not turn it into a crash or a success
        log.exception("could not write the login_failed audit row")
        try:
            store.conn.rollback()
        except Exception:  # noqa: BLE001
            pass


def complete(store, cfg: OidcConfig, query, flow_cookie, now: float, http=None) -> LoginResult:
    """Finish a login from the provider's redirect. Returns a LoginResult (a new session) or raises LoginError after writing login_failed."""
    http = _http(http)
    holder = {}
    try:
        return _complete(store, cfg, query, flow_cookie, now, http, holder)
    except LoginError as err:
        err.provider = holder.get("provider")
        _audit_failure(store, err)
        raise


def _single(query, key):
    value = query.get(key) if isinstance(query, dict) else None
    return value if isinstance(value, str) else None


def _complete(store, cfg, query, flow_cookie, now, http, holder) -> LoginResult:
    state = _single(query, "state")
    if not state or len(state) > 128:
        raise LoginError("flow_invalid", "no state")
    row = store.conn.execute(
        "SELECT state, nonce, code_verifier, provider, next, binder_hash, expires_at FROM auth_flow WHERE state = ?", (state,)).fetchone()
    if row is None:
        raise LoginError("flow_invalid", "unknown or used state")
    # Single use: the delete's row count decides a race between two callbacks carrying the same state.
    used = store.conn.execute("DELETE FROM auth_flow WHERE state = ?", (state,))
    store.conn.commit()
    if used.rowcount != 1:
        raise LoginError("flow_invalid", "state used by another request")
    holder["provider"] = row["provider"]
    if now >= sessions._epoch(row["expires_at"]):
        raise LoginError("flow_invalid", "flow expired")
    if not isinstance(flow_cookie, str) or not flow_cookie or not hmac.compare_digest(_sha(flow_cookie), row["binder_hash"]):
        raise LoginError("flow_invalid", "not the browser that started it")
    provider = cfg.provider(row["provider"])
    if provider is None:
        raise LoginError("flow_invalid", "provider no longer configured")
    if "error" in query:        # fixed message; the provider's text is never shown, stored or reflected
        raise LoginError("provider_error", "provider sent an error")
    code = _single(query, "code")
    if not code or len(code) > 2048:
        raise LoginError("flow_invalid", "no code")

    endpoints = _discover(provider, http, now)
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": cfg.redirect_uri, "client_id": provider.client_id,
            "client_secret": provider.client_secret, "code_verifier": row["code_verifier"]}
    try:
        reply = http.post_form(endpoints["token_endpoint"], form)
    except Exception as exc:  # noqa: BLE001
        log.warning("token endpoint unreachable provider=%s reason=%s", provider.id, exc.__class__.__name__)
        raise LoginError("oidc_unavailable", "token endpoint unreachable") from None
    if not isinstance(reply, dict):
        raise LoginError("oidc_token_invalid", "token response not an object")
    if "error" in reply:
        given = reply.get("error")
        log.warning("token endpoint refused provider=%s error=%s", provider.id, given if isinstance(given, str) and _OAUTH_CODE.fullmatch(given) else "other")
        raise LoginError("oidc_token_invalid", "token endpoint refused")
    token = reply.get("id_token")
    if not isinstance(token, str) or not token:
        raise LoginError("oidc_token_invalid", "no id_token")

    claims = _verify(provider, token, row["nonce"], now, http)
    name, overage = _map(store, provider, claims, now)
    value, csrf = sessions.create(store, name, "oidc:" + provider.id, now)
    store.append_audit(name, "login", "session", name, "oidc:" + provider.id)
    return LoginResult(name, safe_next(row["next"]), provider.id, value, csrf, overage)


def _verify(provider: Provider, token: str, nonce: str, now: float, http) -> dict:
    parts = token.split(".")
    try:
        header = oidc_jwt._json_object(oidc_jwt._b64d(parts[0])) if len(parts) == 3 else None
    except oidc_jwt.TokenError:
        header = None
    if header is None:
        raise LoginError("oidc_token_invalid", "malformed")
    if header.get("alg") != "RS256":          # decided before any key is fetched, so a forged alg costs no outbound call
        raise LoginError("oidc_alg_unsupported", "alg")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise LoginError("oidc_token_invalid", "no kid")
    _ctx.now, _ctx.http = now, http
    try:
        jwks = _jwks_cache(provider).keys(provider.issuer, kid)
        return oidc_jwt.verify_id_token(token, jwks, issuer=provider.issuer, audience=provider.client_id, nonce=nonce, now=now)
    except oidc_jwt.TokenError as exc:
        if exc.code == "unavailable":
            raise LoginError("oidc_unavailable", "keys unavailable") from None
        if exc.code == "alg_unsupported":
            raise LoginError("oidc_alg_unsupported", "alg") from None
        raise LoginError("oidc_token_invalid", exc.code) from None
    finally:
        _ctx.now = _ctx.http = None


# ---- mapping a verified token to a user ----------------------------------------------

def _clean_display(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = " ".join(_CONTROL.sub("", value).split())[:100].strip()
    return text or None


def _allowed(provider: Provider, claims: dict) -> bool:
    """The customer's own boundary: Google's hosted domain, Microsoft's tenant. Applies to every login, known identity or not."""
    if provider.id == "microsoft":
        tid = claims.get("tid")
        return isinstance(tid, str) and tid.lower() in provider.tenants
    if provider.domains:
        hd = claims.get("hd")
        return isinstance(hd, str) and hd.lower() in provider.domains
    return True


def _email_trusted(provider: Provider, claims: dict) -> bool:
    """May this token's email be used to find or create an account? Google: verified, and inside the allowed domain list (which must exist).
    Microsoft: an allowed tenant (its email claim is mutable and unverified, so only a tenant the customer controls makes it usable), and an
    email_verified that is present must be true."""
    if not _allowed(provider, claims):
        return False
    if provider.id == "google":
        return bool(provider.domains) and claims.get("email_verified") is True
    return claims.get("email_verified", True) is True


def _name_from_email(store, email: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._:-]", "_", email.split("@")[0])[:90] or "user"
    n = 1
    while True:
        candidate = base if n == 1 else f"{base[:90]}-{n}"
        taken = candidate.lower() in {r.lower() for r in users.RESERVED} or store.conn.execute(
            "SELECT 1 FROM volt_user WHERE lower(name) = ?", (candidate.lower(),)).fetchone() is not None
        if not taken:
            return candidate
        n += 1


def _create_user(store, email: str, display: Optional[str]) -> str:
    """An auto-provisioned account: viewer, no usable token (the hash is of random bytes nobody sees), source oidc."""
    for _ in range(20):
        name = _name_from_email(store, email)
        try:
            store.conn.execute(
                "INSERT INTO volt_user (name, role, token_hash, created_at, disabled_at, email, display_name, source) VALUES (?, 'viewer', ?, ?, NULL, ?, ?, 'oidc')",
                (name, users.hash_token(secrets.token_urlsafe(48)), users._stamp(), email, display))
            store.conn.commit()
            return name
        except store.IntegrityError:
            store.conn.rollback()
    raise LoginError("not_provisioned", "could not allocate a name")


def _user_row(store, name):
    return store.conn.execute("SELECT name, disabled_at, display_name, source FROM volt_user WHERE name = ?", (name,)).fetchone()


def map_user(store, provider: Provider, claims: dict, now: Optional[float] = None) -> str:
    """The Volt user a verified token signs in as (creating a link or, where allowed, an account); raises LoginError otherwise."""
    return _map(store, provider, claims, now if now is not None else time.time())[0]


def _map(store, provider: Provider, claims: dict, now: float) -> tuple[str, bool]:
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip() or len(sub) > 255 or _CONTROL.search(sub):
        raise LoginError("oidc_token_invalid", "bad sub")
    if not _allowed(provider, claims):
        raise LoginError("not_provisioned", "outside the allowed domain or tenant")
    email = access.clean_email(claims.get("email"))
    display = _clean_display(claims.get("name"))
    stamp = sessions._stamp(now)
    ident = store.conn.execute("SELECT user_name FROM identity WHERE issuer = ? AND sub = ?", (provider.issuer, sub)).fetchone()
    if ident is None:
        name = _link_or_create(store, provider, claims, sub, email, display, stamp)
        ident = store.conn.execute("SELECT user_name FROM identity WHERE issuer = ? AND sub = ?", (provider.issuer, sub)).fetchone()
    if ident is not None:
        name = ident["user_name"]
        user = _user_row(store, name)
        if user is None:
            raise LoginError("not_provisioned", "identity names no user")
        if user["disabled_at"] is not None:
            raise LoginError("user_disabled", "disabled")
        # A known identity logs in as the same user whatever email it now carries; the email is only recorded.
        store.conn.execute("UPDATE identity SET email = ?, display_name = ?, last_login_at = ? WHERE issuer = ? AND sub = ?",
                           (email, display, stamp, provider.issuer, sub))
        if display and (user["source"] == "oidc" or not user["display_name"]):
            store.conn.execute("UPDATE volt_user SET display_name = ? WHERE name = ?", (display, name))
        store.conn.commit()
    overage = _apply_groups(store, provider, name, claims)
    return name, overage


def _link_or_create(store, provider, claims, sub, email, display, stamp) -> str:
    """An identity never seen: link it to an existing unlinked user by trusted email, or (if the provider allows) create one. Else refuse."""
    if email is None or not _email_trusted(provider, claims):
        raise LoginError("not_provisioned", "no trusted email")
    matches = store.conn.execute(
        "SELECT name, disabled_at FROM volt_user WHERE lower(email) = ?", (email.lower(),)).fetchall()
    if len(matches) > 1:
        raise LoginError("not_provisioned", "email is ambiguous")
    if matches:
        user = matches[0]
        if store.conn.execute("SELECT 1 FROM identity WHERE user_name = ? LIMIT 1", (user["name"],)).fetchone() is not None:
            raise LoginError("not_provisioned", "that user is already linked to another identity")
        if user["disabled_at"] is not None:
            raise LoginError("user_disabled", "disabled")
        name = user["name"]
    elif provider.auto_provision:
        name = _create_user(store, email, display)
    else:
        raise LoginError("not_provisioned", "no such user")
    try:
        store.conn.execute(
            "INSERT INTO identity (issuer, sub, user_name, email, display_name, last_login_at) VALUES (?, ?, ?, ?, ?, ?)",
            (provider.issuer, sub, name, email, display, stamp))
        store.conn.commit()
    except store.IntegrityError:        # a concurrent first login linked it: the caller re-reads and takes the known path
        store.conn.rollback()
    return name


def _apply_groups(store, provider: Provider, user_name: str, claims: dict) -> bool:
    """Replace the user's oidc-sourced group membership with what the token's group claim maps to (rows added by an admin are never touched).
    Returns True when the provider sent an overage marker instead of a list: the groups are then left as they were."""
    claim = provider.group_claim
    present = claim in claims
    if not present:
        names = claims.get("_claim_names")
        if (isinstance(names, dict) and claim in names) or claims.get("hasgroups") is True:
            log.warning("group list too large to read from sign-in; leaving the groups of %s as they were", user_name)
            return True
    values = claims.get(claim) if present else []
    if not isinstance(values, list) or len(values) > MAX_GROUP_VALUES or not all(isinstance(v, str) and 0 < len(v) <= 256 for v in values):
        log.warning("group claim is not a usable list; leaving the groups of %s as they were", user_name)
        return False
    desired = set()
    for value in set(values):
        for r in store.conn.execute(
                "SELECT m.group_name FROM oidc_group_map m JOIN volt_group g ON g.name = m.group_name WHERE m.provider = ? AND m.claim_value = ?",
                (provider.id, value)).fetchall():
            desired.add(r["group_name"])
    with store.transaction():
        current = {r["group_name"] for r in store.conn.execute(
            "SELECT group_name FROM group_member WHERE user_name = ? AND source = 'oidc'", (user_name,)).fetchall()}
        for group in current - desired:
            store.conn.execute("DELETE FROM group_member WHERE group_name = ? AND user_name = ? AND source = 'oidc'", (group, user_name))
        for group in desired - current:
            store.conn.execute(
                "INSERT INTO group_member (group_name, user_name, source) VALUES (?, ?, 'oidc') ON CONFLICT (group_name, user_name) DO NOTHING",
                (group, user_name))
    return False
