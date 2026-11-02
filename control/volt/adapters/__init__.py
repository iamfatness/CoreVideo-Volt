"""Device adapters: one contract (base.py), one executor (executor.py), one module per device kind.

`REGISTRY` maps a kind (the cue's room) to (environment variable, module). A module provides `ACTIONS` (command -> ActionSpec, at
module level), `from_text(text)` (build the adapter from the variable's `name=host[:port]` list) and nothing else is needed by the
registry. Modules are imported only when asked for, so nothing is loaded, registered or sent unless its variable is set.
"""

import importlib

REGISTRY: dict = {
    "casparcg": ("VOLT_CASPAR_HOSTS", "volt.adapters.caspar"),
}


def kinds() -> list:
    return sorted(REGISTRY)


def env_names() -> list:
    return [env for env, _mod in REGISTRY.values()]


def module(kind: str):
    return importlib.import_module(REGISTRY[kind][1])


def build(kind: str, text: str):
    """The adapter for `kind` from its environment variable's text. A bad entry is a ValueError."""
    return module(kind).from_text(text)


def actions(kind: str) -> dict:
    """command -> ActionSpec for a kind, without needing the kind to be configured."""
    return module(kind).ACTIONS
