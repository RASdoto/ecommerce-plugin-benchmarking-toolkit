"""
Application config loading + dot-notation resolver.

Ports the Node `BaseConfiguration.#getConfig` / `getDataByDotNotation` logic
(parity items P3/P4): resolve a key as
    config[<dotted-module-path>].<key>   ->   config.default.<key>   ->   hardcoded default
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_app_config(config_path: str | Path) -> dict:
    """Load config.json verbatim (parity P3)."""
    with open(config_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def get_by_dot_notation(obj: Any, dotted: str) -> Any:
    """Walk `obj` by a dot path, coercing numeric segments to list indices.

    Mirrors Node `getDataByDotNotation`. Returns a sentinel `_MISSING` when any
    segment is absent so callers can distinguish "explicitly null" from "absent".
    """
    cur = obj
    for key in dotted.split("."):
        idx: Any
        try:
            idx = int(key)
        except ValueError:
            idx = key
        if isinstance(cur, dict) and idx in cur:
            cur = cur[idx]
        elif isinstance(cur, dict) and isinstance(idx, str):
            # case-insensitive fallback (handles e.g. dir 'Create' vs config 'create')
            match = next((k for k in cur if k.lower() == idx.lower()), None)
            if match is None:
                return _MISSING
            cur = cur[match]
        elif isinstance(cur, list) and isinstance(idx, int) and 0 <= idx < len(cur):
            cur = cur[idx]
        else:
            return _MISSING
    return cur


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<MISSING>"


_MISSING = _Missing()


class ConfigResolver:
    """Resolves settings for a given module path against the app config.

    `module_name` is the dotted module path (e.g. 'fluent-cart.order.create'),
    matching the Node convention of joining the path segments with '.'.
    """

    def __init__(self, app_config: dict, module_name: str):
        self.app_config = app_config
        self.module_name = module_name

    def get(self, key: str, default: Any = None) -> Any:
        # 1. module-specific
        val = get_by_dot_notation(self.app_config, f"{self.module_name}.{key}")
        if val is not _MISSING:
            return val
        # 2. global default block
        val = get_by_dot_notation(self.app_config, f"default.{key}")
        if val is not _MISSING:
            return val
        # 3. hardcoded fallback
        return default

    @property
    def base_url(self) -> str:
        return self.app_config.get("base_url", "") or ""
