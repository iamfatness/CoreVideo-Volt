"""Rate limits, a concurrency gate, quotas and credential-guess lockout.

Everything here lives in the memory of one process, so every limit is per node: N `serve` nodes allow N times a configured rate. The
hosted edge (the proxy) carries the global per-address limit; this is the second line, and it keeps one tenant, one stuck client or
one password guesser from taking the service from the others.

Show control is never shed. `go`, `take`, `hold`, `release`, `kill` and `skip` bypass the token buckets and the concurrency gate (see
SHOW_CONTROL), and `/health` and `/ready` bypass the gate. A quota never applies to a read or to show control: it is checked only where
work or storage is added (ingest, record start).

The token buckets are off unless a tenant deployment turns them on or a `VOLT_RATE_*` variable is set, so an installed single-tenant
Volt behaves as it always did. The gate and the stalled-client timeout are always on."""

from __future__ import annotations

import ipaddress
import math
import threading
import time
from dataclasses import dataclass
from typing import Optional, Sequence


# Show control is the last thing to be shed: none of these is ever rejected by a bucket or the gate.
SHOW_CONTROL = frozenset({"act_go", "act_take", "act_hold", "act_release", "act_kill", "act_skip"})
CLASSES = ("read", "write", "heavy")
# (tokens per second, burst) per principal; a desk polls at 0.5 per second.
DEFAULT_RATES = {"read": (20.0, 40), "write": (5.0, 10), "heavy": (1.0, 3)}
DEFAULT_TENANT_FACTOR = 5          # the tenant aggregate bucket is this many times a principal's
DEFAULT_MAX_CONCURRENT = 64


def rates_from_env(env) -> dict:
    """VOLT_RATE_READ / _WRITE / _HEAVY as "rate,burst" (e.g. "20,40"). Unset names keep their defaults. Raises ValueError."""
    rates = dict(DEFAULT_RATES)
    for cls in CLASSES:
        raw = env.get(f"VOLT_RATE_{cls.upper()}")
        if not raw:
            continue
        try:
            rate_text, burst_text = raw.split(",")
            rate, burst = float(rate_text), int(burst_text)
        except ValueError:
            raise ValueError(f"VOLT_RATE_{cls.upper()} must be 'rate,burst', for example 20,40") from None
        if rate < 0 or burst < 0:
            raise ValueError(f"VOLT_RATE_{cls.upper()} must not be negative")
        rates[cls] = (rate, burst)
    return rates


def any_rate_configured(env) -> bool:
    return any(env.get(f"VOLT_RATE_{c.upper()}") for c in CLASSES)


class TokenBucket:
    def __init__(self, rate: float, burst: int, now=time.monotonic):
        self.rate, self.burst, self._now = float(rate), int(burst), now
        self.tokens, self.at = float(burst), now()

    def take(self) -> tuple:
        """(allowed, seconds until a token is available)."""
        t = self._now()
        self.tokens = min(float(self.burst), self.tokens + max(0.0, t - self.at) * self.rate)
        self.at = t
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0.0
        if self.rate <= 0:
            return False, 60.0
        return False, (1.0 - self.tokens) / self.rate


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: float = 0.0
    kind: str = ""


class Limiter:
    """Token buckets keyed by (tenant, principal) per route class, plus a tenant aggregate that caps all principals together.
    State is in memory, so the limits are per node, not per cluster."""

    def __init__(self, rates: Optional[dict] = None, tenant_factor: int = DEFAULT_TENANT_FACTOR, now=time.monotonic, max_keys: int = 10000):
        self.rates = dict(rates or DEFAULT_RATES)
        self.tenant_factor, self._now, self.max_keys = tenant_factor, now, max_keys
        self._lock = threading.Lock()
        self._buckets: dict = {}

    def size(self) -> int:
        with self._lock:
            return len(self._buckets)

    def _bucket(self, key: tuple, rate: float, burst: int) -> TokenBucket:
        b = self._buckets.get(key)
        if b is None:
            if len(self._buckets) >= self.max_keys:
                self._prune()
            b = self._buckets[key] = TokenBucket(rate, burst, self._now)
        return b

    def _prune(self) -> None:
        """Drop buckets that have refilled completely (an idle principal costs nothing to forget), oldest first, then anything if still full."""
        t = self._now()
        for key in [k for k, b in self._buckets.items() if b.tokens + (t - b.at) * b.rate >= b.burst]:
            del self._buckets[key]
        if len(self._buckets) >= self.max_keys:
            for key in list(self._buckets)[: self.max_keys // 2]:
                del self._buckets[key]

    def check(self, key: tuple, cls: str, fn_name: Optional[str] = None) -> Decision:
        """May this principal do one more `cls` request? `key` is (tenant slug or "", principal). Show control always may."""
        if cls not in CLASSES:
            raise ValueError(f"unknown rate class {cls!r}")
        if fn_name in SHOW_CONTROL:
            return Decision(True)
        rate, burst = self.rates[cls]
        tenant = key[0]
        with self._lock:
            mine = self._bucket((key, cls), rate, burst)
            agg = self._bucket(((tenant, "*"), cls), rate * self.tenant_factor, burst * self.tenant_factor)
            ok, wait = mine.take()
            if not ok:
                return Decision(False, wait, "rate")
            ok, wait = agg.take()
            if not ok:
                mine.tokens = min(float(mine.burst), mine.tokens + 1.0)     # the principal did not use its token
                return Decision(False, wait, "rate")
        return Decision(True)


class ConcurrencyGate:
    """At most `limit` requests in flight per node. Exempt requests (health, readiness, show control) always enter and never take or
    free a normal slot."""

    def __init__(self, limit: int = DEFAULT_MAX_CONCURRENT):
        self.limit = int(limit)
        self._lock = threading.Lock()
        self._normal = 0
        self._exempt = 0

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._normal + self._exempt

    def enter(self, exempt: bool = False) -> bool:
        with self._lock:
            if exempt:
                self._exempt += 1
                return True
            if self._normal >= self.limit:
                return False
            self._normal += 1
            return True

    def leave(self, exempt: bool = False) -> None:
        with self._lock:
            if exempt:
                self._exempt = max(0, self._exempt - 1)
            else:
                self._normal = max(0, self._normal - 1)


class FailureTracker:
    """Failed bearer checks per client address. `max_failures` in `window` seconds blocks that address for `block` seconds. A success does
    not clear the count (a guesser who finds one right token must not be forgiven the rest). Direct loopback peers are exempt when
    `exempt_loopback` is set: they are the machine's own operator, and behind a proxy the real address comes from X-Forwarded-For
    (VOLT_TRUSTED_PROXIES)."""

    def __init__(self, window: float = 60.0, max_failures: int = 10, block: float = 60.0, now=time.monotonic,
                 max_addresses: int = 5000, exempt_loopback: bool = False):
        self.window, self.max_failures, self.block, self._now = window, max_failures, block, now
        self.max_addresses, self.exempt_loopback = max_addresses, exempt_loopback
        self._lock = threading.Lock()
        self._fails: dict = {}
        self._until: dict = {}

    def size(self) -> int:
        with self._lock:
            return len(self._fails) + len(self._until)

    def _exempt(self, address: str) -> bool:
        if not self.exempt_loopback:
            return False
        try:
            return ipaddress.ip_address(address).is_loopback
        except ValueError:
            return False

    def failed(self, address: str) -> None:
        if self._exempt(address):
            return
        t = self._now()
        with self._lock:
            if len(self._fails) >= self.max_addresses:
                self._fails = {a: v for a, v in self._fails.items() if v and t - v[-1] < self.window}
                if len(self._fails) >= self.max_addresses:
                    self._fails.clear()
            hits = [x for x in self._fails.get(address, []) if t - x < self.window]
            hits.append(t)
            self._fails[address] = hits
            if len(hits) >= self.max_failures:
                self._until[address] = t + self.block
                self._fails.pop(address, None)

    def succeeded(self, address: str) -> None:
        """Deliberately does nothing to the count; kept so callers read naturally."""

    def blocked(self, address: str) -> bool:
        t = self._now()
        with self._lock:
            until = self._until.get(address)
            if until is None:
                return False
            if t >= until:
                del self._until[address]
                return False
            return True


def parse_trusted(text: str) -> list:
    """VOLT_TRUSTED_PROXIES: comma-separated IP addresses or CIDR ranges. Host names are refused (a name can change; an address is what the
    socket shows)."""
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ipaddress.ip_network(part, strict=False)
        except ValueError:
            raise ValueError(f"VOLT_TRUSTED_PROXIES entries are IP addresses or CIDR ranges, not {part!r}") from None
        out.append(part)
    return out


def _trusted(address: str, trusted: Sequence[str]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(t, strict=False) for t in trusted)


def client_address(peer: str, headers, trusted: Sequence[str]) -> str:
    """The socket peer, unless the peer is a trusted proxy: then the first X-Forwarded-For entry (counting from the right) that is not
    itself trusted. A header from an untrusted peer is ignored, and so is anything that is not an IP address."""
    if not trusted or not _trusted(peer, trusted):
        return peer
    raw = headers.get("X-Forwarded-For") if hasattr(headers, "get") else None
    if not raw:
        return peer
    for entry in reversed([e.strip() for e in raw.split(",")]):
        try:
            ipaddress.ip_address(entry)
        except ValueError:
            return peer
        if not _trusted(entry, trusted):
            return entry
    return peer


class QuotaExceeded(Exception):
    def __init__(self, name: str, limit: int):
        super().__init__(f"{name} limit of {limit} reached")
        self.name, self.limit = name, limit


QUOTAS = ("max_assets", "max_jobs_per_day")     # enforced here; max_running_jobs is the runner's cap, max_storage_bytes needs a size column


def check_quota(store, limits, name: str, adding: int = 1, now: Optional[str] = None) -> None:
    """Raise QuotaExceeded when adding `adding` would pass the tenant's limit for `name`. No limit (or no tenancy: limits None) passes."""
    if name not in QUOTAS:
        raise ValueError(f"unknown quota {name!r}")
    limit = (limits or {}).get(name)
    if limit is None:
        return
    if name == "max_assets":
        used = store.conn.execute("SELECT COUNT(*) AS n FROM asset WHERE deleted_at IS NULL").fetchone()["n"]
    else:
        from datetime import datetime, timedelta, timezone
        end = datetime.fromisoformat((now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")).replace("Z", "+00:00"))
        since = (end - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        used = store.conn.execute("SELECT COUNT(*) AS n FROM job WHERE created_at >= ?", (since,)).fetchone()["n"]
    if used + adding > limit:
        raise QuotaExceeded(name, int(limit))


def retry_seconds(wait: float) -> int:
    return max(1, int(math.ceil(wait)))

