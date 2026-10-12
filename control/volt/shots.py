"""Shot spans. A scene cut is a span on the same asset."""

from __future__ import annotations

import subprocess
from pathlib import Path

from volt import timecode
from volt.store import Store, _id, _now


class ShotError(Exception):
    pass


def index_shots(store: Store, asset_id: str, cuts: list[float] | None = None, threshold: float = 0.4) -> int:
    """Write one shot span per scene. Replacing is the same set, not a second set.

    `cuts` are seconds from the start of the file. Without them, ffmpeg's scene filter finds the cuts.
    An open file is refused. The first shot starts at the essence timecode."""
    essence = store.conn.execute(
        "SELECT location, open, frame_rate, timecode_start FROM essence WHERE asset_id = ? AND role = 'hi-res'",
        (asset_id,),
    ).fetchone()
    if essence is None:
        raise ShotError(f"no hi-res for {asset_id}")
    if essence["open"]:
        raise ShotError("asset is still recording; wrap it first")
    found = cuts if cuts is not None else detect(Path(essence["location"]), threshold)
    rate = timecode.parse_rate(essence["frame_rate"] or "25")
    drop = timecode.is_drop(essence["timecode_start"])
    origin = timecode.tc_to_frames(essence["timecode_start"] or "00:00:00:00", rate, drop)
    bounds = [0.0, *[c for c in found if c > 0], None]
    rows = []
    for index, start in enumerate(bounds[:-1]):
        end = bounds[index + 1]
        f_in = origin + timecode.seconds_to_frames(start, rate)
        f_out = origin + (timecode.seconds_to_frames(end, rate) if end is not None else f_in - origin + 1)
        if f_out <= f_in:
            f_out = f_in + 1
        rows.append((
            timecode.frames_to_tc(f_in, rate, drop),
            timecode.frames_to_tc(f_out, rate, drop),
            f"shot {index + 1}",
        ))
    with store.transaction(f"shots:{asset_id}"):
        store.conn.execute("DELETE FROM span WHERE asset_id = ? AND kind = 'shot'", (asset_id,))
        stamp = _now()
        for tc_in, tc_out, text in rows:
            store.conn.execute(
                """INSERT INTO span (id, asset_id, kind, tc_in, tc_out, text, created_at)
                   VALUES (?, ?, 'shot', ?, ?, ?, ?)""",
                (_id(), asset_id, tc_in, tc_out, text, stamp),
            )
    return len(rows)


def detect(path: Path, threshold: float) -> list[float]:
    """Scene-cut times in seconds, from ffmpeg. The first frame is not a cut."""
    probe = subprocess.run(
        [
            "ffmpeg", "-i", str(path),
            "-filter:v", f"select='gt(scene,{threshold})',showinfo",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0 and "pts_time" not in probe.stderr:
        raise ShotError(probe.stderr[-400:].strip() or "scene detect failed")
    cuts = []
    for line in probe.stderr.splitlines():
        if "pts_time:" not in line:
            continue
        for part in line.split():
            if part.startswith("pts_time:"):
                cuts.append(float(part.split(":", 1)[1]))
    return cuts


def shots(store: Store, asset_id: str) -> list[tuple[str, str, str]]:
    rows = store.conn.execute(
        "SELECT tc_in, tc_out, text FROM span WHERE asset_id = ? AND kind = 'shot' ORDER BY tc_in",
        (asset_id,),
    ).fetchall()
    return [(row["tc_in"], row["tc_out"], row["text"]) for row in rows]
