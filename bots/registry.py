from __future__ import annotations

from dataclasses import dataclass
import importlib
import pkgutil
from typing import Callable, Dict, List, Optional

import chess


@dataclass(frozen=True)
class BotEngine:
    key: str
    name: str
    choose_move: Callable[[chess.Board], chess.Move]
    description: str = ""


_ENGINES: Dict[str, BotEngine] = {}
_LOADED = False


def register(engine: BotEngine) -> None:
    if not engine.key or not isinstance(engine.key, str):
        raise ValueError("Bot engine key must be a non-empty string.")
    if engine.key in _ENGINES:
        raise ValueError(f"Bot engine key already registered: {engine.key}")
    _ENGINES[engine.key] = engine


def _ensure_loaded() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True

    import bots  # local package

    for module_info in pkgutil.iter_modules(bots.__path__):
        name = module_info.name
        if name.startswith("_") or name in {"registry"}:
            continue
        importlib.import_module(f"bots.{name}")


def get_engine(key: str) -> Optional[BotEngine]:
    _ensure_loaded()
    return _ENGINES.get(key)


def list_engines() -> List[dict]:
    _ensure_loaded()
    return [
        {
            "key": e.key,
            "name": e.name,
            "description": e.description or "",
        }
        for e in sorted(_ENGINES.values(), key=lambda x: x.key)
    ]

