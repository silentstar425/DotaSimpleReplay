"""同一比赛编号（match_id）关联的录像文件登记表：临时下载、解压后的 .dem、可选归档路径。

用于删除任务或本机记录时一并清理缓存与磁盘文件，避免漏删。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from replay_cache import cache_path_for_dem, delete_replay_cache
from replay_download_io import hint_match_id_from_filename

ASSETS_JSON = Path(__file__).resolve().parent / ".dsr_replay_assets.json"
_lock = threading.Lock()


def _norm_path(p: Path) -> str:
    try:
        return str(p.resolve()).replace("\\", "/").lower()
    except OSError:
        return str(p).replace("\\", "/").lower()


def _load() -> dict[str, Any]:
    try:
        if ASSETS_JSON.is_file():
            raw = json.loads(ASSETS_JSON.read_text(encoding="utf-8"))
            rep = raw.get("replays")
            if isinstance(rep, dict):
                return {"replays": dict(rep)}
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return {"replays": {}}


def _save(data: dict[str, Any]) -> None:
    ASSETS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _mid_key(match_id: int) -> str:
    return f"mid:{int(match_id)}"


def register_download_tmp(match_id: int, task_id: str, tmp: Path) -> None:
    with _lock:
        d = _load()
        k = _mid_key(match_id)
        rec = d["replays"].get(k)
        if not isinstance(rec, dict):
            rec = {}
        rec["match_id"] = int(match_id)
        rec["task_id"] = str(task_id)
        rec["download_tmp"] = str(tmp.resolve())
        d["replays"][k] = rec
        _save(d)


def register_dem_ready(match_id: int, task_id: str, dem: Path) -> None:
    with _lock:
        d = _load()
        k = _mid_key(match_id)
        rec = d["replays"].get(k)
        if not isinstance(rec, dict):
            rec = {}
        rec["match_id"] = int(match_id)
        rec["task_id"] = str(task_id)
        try:
            dp = dem.resolve()
        except OSError:
            dp = dem
        rec["dem"] = str(dp)
        try:
            rec["cache"] = str(cache_path_for_dem(dp))
        except OSError:
            rec.pop("cache", None)
        rec.pop("download_tmp", None)
        d["replays"][k] = rec
        _save(d)


def _unlink_file(p: Path) -> None:
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass


def purge_match(match_id: int) -> None:
    """按 match_id 删除登记表中记载的 dem / 临时文件 / 归档，并删除解析缓存，最后移除登记项。"""
    with _lock:
        d = _load()
        k = _mid_key(match_id)
        rec = d["replays"].pop(k, None)
        _save(d)
    if not isinstance(rec, dict):
        return
    dem_for_cache: Path | None = None
    paths: list[Path] = []
    for field in ("dem", "archive", "download_tmp", "cache"):
        v = rec.get(field)
        if isinstance(v, str) and v.strip():
            paths.append(Path(v))
    for p in paths:
        name = p.name.lower()
        if name.endswith(".dem") and not name.endswith(".dem.bz2"):
            try:
                dem_for_cache = p.resolve()
            except OSError:
                dem_for_cache = p
            break
    if dem_for_cache is not None:
        try:
            delete_replay_cache(dem_for_cache)
        except OSError:
            pass
    for p in paths:
        _unlink_file(p)


def purge_local_replay_dem_file(dem_path: Path) -> None:
    """删除本机库中的一个 .dem：若登记表中该 match 的 dem 正是此文件，则按登记表整组清理。"""
    try:
        p = dem_path.resolve()
    except OSError:
        p = dem_path
    key = _norm_path(p)
    mid = hint_match_id_from_filename(p)
    matched_registry = False
    if mid is not None:
        with _lock:
            d = _load()
            rec = d["replays"].get(_mid_key(mid))
        if isinstance(rec, dict):
            dem_s = rec.get("dem")
            if isinstance(dem_s, str) and dem_s.strip() and _norm_path(Path(dem_s)) == key:
                matched_registry = True
    if matched_registry and mid is not None:
        purge_match(mid)
        return
    try:
        delete_replay_cache(p)
    except OSError:
        pass
    _unlink_file(p)
