"""Locations that travel with the installed volt package."""

from __future__ import annotations

from pathlib import Path

import volt


def package_root() -> Path:
    return Path(volt.__file__).resolve().parent
