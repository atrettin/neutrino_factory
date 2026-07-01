from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ConfigTranslator(ABC):
    name = "base"

    @abstractmethod
    def translate(self, config: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
