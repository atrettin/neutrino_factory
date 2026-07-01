from __future__ import annotations

from typing import Any

from .genie import GenieAdapter
from .gibuu import GiBUUAdapter
from .neut import NeutAdapter
from .nuwro import NuWroAdapter


REGISTRY = {
    "genie": GenieAdapter,
    "gibuu": GiBUUAdapter,
    "neut": NeutAdapter,
    "nuwro": NuWroAdapter,
}


def get_adapter(name: str, config: dict[str, Any]):
    try:
        adapter_cls = REGISTRY[name.lower()]
    except KeyError as error:
        raise ValueError(f"Unsupported generator '{name}'") from error
    return adapter_cls(config)
