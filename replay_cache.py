#!/usr/bin/env python3
"""
回放解析缓存工具。

缓存目标：
- 将 DEM 解析后的播放必要数据持久化到本地文件
- 下次播放同一 DEM 时直接读取，减少重复解析时间
"""

from __future__ import annotations

import hashlib
import pickle
from pathlib import Path
from typing import Any


CACHE_VERSION = 4
CACHE_DIRNAME = ".replay_cache"


def _dem_signature(dem_path: Path) -> str:
    resolved = dem_path.resolve()
    stat = resolved.stat()
    raw = f"{resolved}|{stat.st_size}|{stat.st_mtime_ns}|v{CACHE_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def cache_path_for_dem(dem_path: Path) -> Path:
    key = _dem_signature(dem_path)
    cache_dir = Path(CACHE_DIRNAME).resolve()
    return cache_dir / f"{key}.pkl"


def load_replay_cache(dem_path: Path) -> dict[str, Any] | None:
    path = cache_path_for_dem(dem_path)
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            obj = pickle.load(f)
        if not isinstance(obj, dict):
            return None
        if obj.get("cache_version") != CACHE_VERSION:
            return None
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            return None
        return payload
    except Exception:
        return None


def save_replay_cache(dem_path: Path, payload: dict[str, Any]) -> Path:
    path = cache_path_for_dem(dem_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    wrapper = {
        "cache_version": CACHE_VERSION,
        "source_dem": str(dem_path.resolve()),
        "payload": payload,
    }
    with tmp.open("wb") as f:
        pickle.dump(wrapper, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)
    return path


def delete_replay_cache(dem_path: Path) -> bool:
    path = cache_path_for_dem(dem_path)
    if not path.exists():
        return False
    path.unlink()
    return True

