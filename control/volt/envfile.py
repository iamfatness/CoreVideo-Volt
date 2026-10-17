"""A tiny KEY=VALUE environment file, for services (`volt run --env-file`, `volt check --env-file`).

One format on every OS: blank lines and lines starting with `#` are ignored, a leading `export ` is ignored,
a value may be wrapped in one pair of single or double quotes, and nothing is expanded or unescaped (a Windows
path keeps its backslashes). Variables already set in the real environment win over the file, so a one-off
`VOLT_PORT=9000 volt run --env-file ...` does what it says.
"""

from __future__ import annotations

import re
from pathlib import Path

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvFileError(Exception):
    pass


def parse(text: str, source: str = "env file") -> dict:
    values: dict = {}
    for number, raw in enumerate(text.lstrip("﻿").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.match(key):
            raise EnvFileError(f"{source} line {number}: expected KEY=VALUE")  # never echo the line: it may hold a secret
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load(path, environ: dict) -> dict:
    """The file's variables that `environ` does not already set. Raises EnvFileError if it cannot be read."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise EnvFileError(f"cannot read {path}: {exc.strerror or exc}") from exc
    return {k: v for k, v in parse(text, str(path)).items() if k not in environ}
