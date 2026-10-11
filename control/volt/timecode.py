"""The one timecode library. Non-drop and drop-frame, on exact rational frame rates.

A frame rate is a Fraction (30000/1001, not 29.97). Everything is counted in whole frames and only turned
into seconds at the edge, as an exact Fraction, so nothing drifts. Drop-frame labels use `;` before the
frames; they exist only for 30000/1001 (drops 2 label numbers a minute, except every tenth minute) and
60000/1001 (drops 4). Drop-frame drops labels, never frames."""

from __future__ import annotations

import re
from functools import lru_cache
from fractions import Fraction


class TimecodeError(ValueError):
    pass


# Rates people write as decimals. Any other decimal must be a whole number or is refused as ambiguous.
_NAMED = (Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001), Fraction(120000, 1001))
_DROP_PER_MINUTE = {Fraction(30000, 1001): 2, Fraction(60000, 1001): 4}


@lru_cache(maxsize=64)
def parse_rate(rate) -> Fraction:
    """Fraction, int, "30000/1001", "25", or a decimal such as 29.97 (snapped to its broadcast rate)."""
    try:
        if isinstance(rate, float) or (isinstance(rate, str) and "/" not in rate):
            value = float(rate)
            for exact in _NAMED:
                if abs(value - float(exact)) < 0.0015:
                    return exact
            if value != int(value):
                raise TimecodeError(f"ambiguous frame rate {rate!r}; write it as a ratio such as 30000/1001")
            frac = Fraction(int(value))
        else:
            frac = Fraction(rate)
    except TimecodeError:
        raise
    except (ValueError, ZeroDivisionError, TypeError, OverflowError) as exc:
        raise TimecodeError(f"bad frame rate {rate!r}") from exc
    if frac <= 0:
        raise TimecodeError(f"bad frame rate {rate!r}")
    return frac


@lru_cache(maxsize=64)
def nominal_fps(rate) -> int:
    """The frame count a timecode second counts to: 30 for 30000/1001, 25 for 25."""
    return round(parse_rate(rate))


@lru_cache(maxsize=64)
def drop_per_minute(rate) -> int:
    """Labels dropped per non-tenth minute. Raises if the rate has no drop-frame form."""
    r = parse_rate(rate)
    if r not in _DROP_PER_MINUTE:
        raise TimecodeError(f"drop-frame is defined only for 30000/1001 and 60000/1001, not {r}")
    return _DROP_PER_MINUTE[r]


_TC = re.compile(r"^(\d{2,}):(\d{2}):(\d{2})([:;.])(\d{2,3})$")


def is_drop(tc: str | None) -> bool:
    """A `;` (or `.`) before the frames marks drop-frame."""
    m = _TC.match(str(tc or "").strip())
    return bool(m) and m.group(4) in ";."


def tc_to_frames(tc: str, rate, drop: bool | None = None) -> int:
    """Frame count since 00:00:00:00. `drop=None` reads the separator; an explicit flag overrides it (so
    a non-drop clip's span labels may be written with either separator). Illegal labels raise."""
    m = _TC.match(str(tc).strip())
    if not m:
        raise TimecodeError(f"bad timecode {tc!r}")
    h, mi, s, sep, f = int(m[1]), int(m[2]), int(m[3]), m[4], int(m[5])
    r = parse_rate(rate)
    nom = nominal_fps(r)
    if drop is None:
        drop = sep in ";."
    if mi > 59 or s > 59:
        raise TimecodeError(f"bad timecode {tc!r}: minutes and seconds run 00-59")
    if f >= nom:
        raise TimecodeError(f"bad timecode {tc!r}: frames run 0-{nom - 1} at {r}")
    total = ((h * 60 + mi) * 60 + s) * nom + f
    if not drop:
        return total
    d = drop_per_minute(r)
    if s == 0 and f < d and mi % 10 != 0:
        raise TimecodeError(f"{tc!r} does not exist in drop-frame: frames 0-{d - 1} are skipped at the start "
                            f"of every minute except each tenth")
    minutes = h * 60 + mi
    return total - d * (minutes - minutes // 10)


def frames_to_tc(frames: int, rate, drop: bool = False) -> str:
    """Label for a frame count. Drop-frame uses `;` before the frames. Hours are not wrapped at 24."""
    if frames < 0:
        raise TimecodeError("negative frame count")
    r = parse_rate(rate)
    nom = nominal_fps(r)
    n = int(frames)
    sep = ":"
    if drop:
        d = drop_per_minute(r)
        per_min = nom * 60 - d
        per_10 = nom * 600 - 9 * d
        tens, rem = divmod(n, per_10)
        n += 9 * d * tens + (d * ((rem - d) // per_min) if rem >= d else 0)
        sep = ";"
    f = n % nom
    secs = n // nom
    return f"{secs // 3600:02d}:{secs // 60 % 60:02d}:{secs % 60:02d}{sep}{f:02d}"


def frames_to_seconds(frames: int, rate) -> Fraction:
    return Fraction(frames) / parse_rate(rate)


def seconds_to_frames(seconds, rate) -> int:
    """Nearest frame (ties to even). Floats are read as their shortest decimal (0.9 is 9/10)."""
    if isinstance(seconds, float):
        seconds = Fraction(repr(seconds))  # 0.9 means nine tenths, not the binary neighbour of it
    return round(Fraction(seconds) * parse_rate(rate))


def tc_to_seconds_exact(tc: str, rate, drop: bool | None = None) -> Fraction:
    return frames_to_seconds(tc_to_frames(tc, rate, drop), rate)


def tc_to_seconds(tc: str, rate, drop: bool | None = None) -> float:
    return float(tc_to_seconds_exact(tc, rate, drop))


def seconds_to_tc(seconds, rate, drop: bool = False) -> str:
    return frames_to_tc(seconds_to_frames(seconds, rate), rate, drop)
