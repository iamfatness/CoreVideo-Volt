"""Volt control plane. The version below is the one `volt version` and /health report; keep it equal to
the VERSION file at the repository root (a test checks) and add a CHANGELOG.md entry per release."""

import logging as _logging

__version__ = "0.1.0"

# Library rule: say nothing unless a process (volt.logs.setup) installs handlers.
_logging.getLogger("volt").addHandler(_logging.NullHandler())
