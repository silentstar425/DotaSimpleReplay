"""录像下载：OpenDota 元数据 + Valve CDN，解压与文件探测（供 run.py 与下载管理器共用）。"""

from __future__ import annotations

import bz2
import gzip
import json
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPLAY_DL_UA = "Mozilla/5.0 (compatible; DotaSimpleReplay/1.0)"


def replay_storage_root() -> Path:
    """录像存放目录：项目根下 replays/（仅 .dem）。"""
    return Path(__file__).resolve().parent / "replays"


def ensure_replay_storage_dir() -> Path:
    p = replay_storage_root()
    p.mkdir(parents=True, exist_ok=True)
    return p


def legacy_replay_samples_dir() -> Path:
    """旧目录名；启动时会迁移到 replays/ 并尝试删除。"""
    return Path(__file__).resolve().parent / "replay_samples"


def replay_library_roots() -> list[Path]:
    roots: list[Path] = []
    p = replay_storage_root()
    try:
        r = p.resolve()
    except OSError:
        return roots
    roots.append(r)
    return roots


def is_replay_library_path(path: Path) -> bool:
    try:
        p = path.expanduser().resolve()
    except OSError:
        return False
    if not p.is_file():
        return False
    name = p.name.lower()
    if not (name.endswith(".dem") and not name.endswith(".dem.bz2")):
        return False
    for root in replay_library_roots():
        try:
            p.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def iter_default_replay_candidates() -> list[Path]:
    """无命令行参数时：扫描 replays/ 下 *.dem（不含 .dem.bz2）。"""
    out: list[Path] = []
    seen: set[str] = set()
    root = replay_storage_root()
    if not root.is_dir():
        return out
    for p in sorted(root.glob("*.dem")):
        if not p.is_file():
            continue
        if p.name.startswith("_dl_"):
            continue
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


_RE_MATCH_ID_TAIL = re.compile(r"_(\d{6,})\.(?:dem|dem\.bz2)$", re.IGNORECASE)
_RE_MATCH_ID_ONLY = re.compile(r"^(\d{6,})\.dem(?:\.bz2)?$", re.IGNORECASE)


def hint_match_id_from_filename(path: Path) -> int | None:
    name = path.name
    m = _RE_MATCH_ID_TAIL.search(name)
    if m:
        return int(m.group(1))
    m2 = _RE_MATCH_ID_ONLY.match(name)
    if m2:
        return int(m2.group(1))
    return None


def list_stored_dem_files() -> list[dict[str, Any]]:
    """列出 replays/ 下已有录像（仅 .dem），供下载管理 UI 展示。"""
    items: list[dict[str, Any]] = []
    root = replay_storage_root()
    try:
        root = root.resolve()
    except OSError:
        return items
    if not root.is_dir():
        return items
    for p in sorted(root.glob("*.dem")):
        if not p.is_file():
            continue
        if p.name.startswith("_dl_"):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        key = str(p.resolve())
        mid = hint_match_id_from_filename(p)
        items.append(
            {
                "path": key,
                "name": p.name,
                "size": st.st_size,
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                "folder": "replays",
                "match_id_hint": mid,
            }
        )
    items.sort(key=lambda x: x.get("modified") or "", reverse=True)
    return items


def migrate_legacy_replay_samples_to_replays() -> None:
    """将 replay_samples/ 下录像迁入 replays/ 并删除旧目录（.dem.bz2 解压为 .dem 后删除压缩包）。"""
    legacy = legacy_replay_samples_dir()
    if not legacy.is_dir():
        return
    dest_root = ensure_replay_storage_dir()
    for p in sorted(legacy.iterdir()):
        if not p.is_file():
            continue
        if p.name.startswith("_dl_"):
            continue
        name_l = p.name.lower()
        try:
            if name_l.endswith(".dem") and not name_l.endswith(".dem.bz2"):
                target = dest_root / p.name
                if target.exists():
                    continue
                shutil.move(str(p), str(target))
                continue
            if name_l.endswith(".dem.bz2"):
                dem_out = dest_root / (p.name[:-4])
                if dem_out.exists():
                    p.unlink(missing_ok=True)
                    continue
                materialize_downloaded_to_dem(p, dem_out)
        except OSError as ex:
            print(f"[migrate] 跳过 {p}: {ex}")
    try:
        legacy.rmdir()
        print(f"[migrate] 已删除空目录 replay_samples/")
    except OSError:
        try:
            shutil.rmtree(legacy, ignore_errors=False)
            print(f"[migrate] 已删除目录 replay_samples/")
        except OSError as ex:
            print(f"[migrate] 未能删除 replay_samples/（可能仍有非录像文件）: {ex}")


def extract_replays_bz2_archives() -> None:
    """每次启动：解压 replays/ 下遗留的 .bz2（含 .dem.bz2），成功后删除压缩包。"""
    root = replay_storage_root()
    if not root.is_dir():
        return
    for p in sorted(root.glob("*.bz2")):
        if not p.is_file() or p.name.startswith("_dl_"):
            continue
        name_l = p.name.lower()
        try:
            if name_l.endswith(".dem.bz2"):
                dem_out = p.parent / (p.name[:-4])
            else:
                dem_out = p.parent / f"{p.stem}.dem"
            if dem_out.exists():
                p.unlink(missing_ok=True)
                print(f"[bz2] 已存在目标，跳过解压并移除压缩包: {p.name}")
                continue
            materialize_downloaded_to_dem(p, dem_out)
            print(f"[bz2] 已解压: {p.name} -> {dem_out.name}")
        except OSError as ex:
            print(f"[bz2] 跳过 {p.name}: {ex}")
        except RuntimeError as ex:
            print(f"[bz2] 跳过 {p.name}: {ex}")


def fetch_opendota_match(match_id: int) -> dict[str, Any]:
    url = f"https://api.opendota.com/api/matches/{int(match_id)}"
    req = urllib.request.Request(url, headers={"User-Agent": REPLAY_DL_UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("OpenDota 无该比赛记录（请确认比赛编号）") from e
        raise RuntimeError(f"OpenDota 请求失败: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 OpenDota：{e}") from e


def valve_replay_download_url(cluster: int, match_id: int, replay_salt: int) -> str:
    return f"http://replay{int(cluster)}.valve.net/570/{int(match_id)}_{int(replay_salt)}.dem.bz2"


def download_url_to_file(url: str, dest: Path, timeout_sec: int = 600) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": REPLAY_DL_UA})
    tmp = dest.parent / (dest.name + ".part")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp, tmp.open("wb") as out:
            shutil.copyfileobj(resp, out, length=1024 * 1024)
        tmp.replace(dest)
    except urllib.error.HTTPError:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    except urllib.error.URLError as e:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise RuntimeError(f"下载连接失败：{e}") from e
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def sniff_downloaded_kind(path: Path) -> str:
    with path.open("rb") as f:
        head = f.read(12)
    if head.startswith(b"BZh"):
        return "bz2"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head.startswith(b"PBDEMS2") or head.startswith(b"PBDEMS"):
        return "dem"
    if head.lstrip().startswith(b"<") or head.startswith(b"<!"):
        return "html"
    return "unknown"


def materialize_downloaded_to_dem(downloaded: Path, dem_out: Path) -> None:
    """将下载文件解压/移动为 dem_out，并删除 downloaded。"""
    dem_out.parent.mkdir(parents=True, exist_ok=True)
    kind = sniff_downloaded_kind(downloaded)
    if kind == "bz2":
        with bz2.open(downloaded, "rb") as src, dem_out.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        downloaded.unlink()
        return
    if kind == "gzip":
        with gzip.open(downloaded, "rb") as src, dem_out.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        downloaded.unlink()
        return
    if kind == "dem":
        if downloaded.resolve() != dem_out.resolve():
            if dem_out.exists():
                dem_out.unlink()
            downloaded.replace(dem_out)
        return
    if kind == "html":
        downloaded.unlink(missing_ok=True)
        raise RuntimeError("下载内容不是录像文件（疑似错误页）")
    downloaded.unlink(missing_ok=True)
    raise RuntimeError("下载文件格式无法识别（需为 .dem.bz2 / gzip / 原始 .dem）")


def sanitize_stem(name: str) -> str:
    s = (name or "").strip() or "replay"
    for c in '<>:"/\\|?*\x00':
        s = s.replace(c, "_")
    s = s.strip(" .") or "replay"
    return s[:120]


def download_match_replay_to_dem(match_id: int) -> Path:
    """一次性下载并解压为 replays/{match_id}.dem。"""
    mid = int(match_id)
    if mid <= 0:
        raise RuntimeError("比赛编号无效")
    m = fetch_opendota_match(mid)
    if not isinstance(m, dict):
        raise RuntimeError("OpenDota 返回数据异常")
    cluster = m.get("cluster")
    salt = m.get("replay_salt")
    if cluster is None or salt is None:
        raise RuntimeError("该比赛无公开回放元数据（replay_salt/cluster 缺失），无法下载")
    url = valve_replay_download_url(int(cluster), mid, int(salt))
    replay_dir = ensure_replay_storage_dir()
    raw = replay_dir / f"{mid}.download"
    print(f"[info] 下载录像: {url}")
    try:
        download_url_to_file(url, raw)
    except urllib.error.HTTPError as e:
        raw.unlink(missing_ok=True)
        raise RuntimeError(f"Valve 录像地址下载失败: HTTP {e.code}") from e
    except Exception:
        raw.unlink(missing_ok=True)
        raise
    if raw.stat().st_size < 4096:
        raw.unlink(missing_ok=True)
        raise RuntimeError("下载文件过小，可能不是有效录像")
    dem = replay_dir / f"{mid}.dem"
    materialize_downloaded_to_dem(raw, dem)
    print(f"[info] 录像已就绪: {dem} ({dem.stat().st_size} bytes)")
    return dem
