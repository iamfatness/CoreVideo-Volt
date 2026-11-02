"""Device adapters: one contract (base.py), one executor (executor.py), one module per device kind.

`REGISTRY` maps a kind to a factory `(hosts: dict) -> Adapter`; roomreg.from_env builds one only when its environment variable is set.
"""

REGISTRY: dict = {}
