"""Configuration loader. Plan §2.3.

Loads ``config.toml`` and deep-merges an optional, gitignored
``config.local.toml`` overlay (machine-specific paths + secrets). Returns a
plain nested dict; callers index sections directly (``cfg["agent"]["port"]``).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib as _toml

    def _load(p: Path) -> dict[str, Any]:
        with p.open("rb") as f:
            return _toml.load(f)
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    import tomli as _toml  # type: ignore

    def _load(p: Path) -> dict[str, Any]:
        with p.open("rb") as f:
            return _toml.load(f)


_HERE = Path(__file__).resolve().parent


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config(path: str | Path = _HERE / "config.toml") -> dict[str, Any]:
    """Load config.toml and overlay config.local.toml if present."""
    path = Path(path)
    cfg = _load(path)
    local = path.with_name("config.local.toml")
    if local.exists():
        cfg = _deep_merge(cfg, _load(local))
    return cfg
