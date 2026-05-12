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
    """新录像默认存放目录：项目根下 replays/（与旧 replay_samples/ 并存扫描）。"""
    return Path(__file__).resolve().parent / "replays"


def ensure_replay_storage_dir() -> Path:
    p = replay_storage_root()
    p.mkdir(parents=True, exist_ok=True)
    return p


def legacy_replay_samples_dir() -> Path:
    return Path(__file__).resolve().parent / "replay_samples"


def replay_samples_dir() -> Path:
    """兼容旧名：等同于 replay_storage_root()，新下载写入 replays/。"""
    return replay_storage_root()


def replay_library_roots() -> list[Path]:
    roots: list[Path] = []
    for p in (replay_storage_root(), legacy_replay_samples_dir()):
        try:
            r = p.resolve()
        except OSError:
            continue
        if r not in roots:
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
    if not (name.endswith(".dem") or name.endswith(".dem.bz2")):
        return False
    for root in replay_library_roots():
        try:
            p.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def iter_default_replay_candidates() -> list[Path]:
    """无命令行参数时：优先 replays/，其次 replay_samples/；排除下载临时文件。"""
    out: list[Path] = []
    seen: set[str] = set()
    for root in (replay_storage_root(), legacy_replay_samples_dir()):
        if not root.is_dir():
            continue
        for pat in ("*.dem", "*.dem.bz2"):
            for p in sorted(root.glob(pat)):
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
    """列出库目录内已有录像（.dem / .dem.bz2），供下载管理 UI 展示。"""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    scanned_dirs: set[Path] = set()
    for root_base, label in (
        (replay_storage_root(), "replays"),
        (legacy_replay_samples_dir(), "replay_samples"),
    ):
        try:
            root = root_base.resolve()
        except OSError:
            continue
        if root in scanned_dirs or not root.is_dir():
            continue
        scanned_dirs.add(root)
        for pat in ("*.dem", "*.dem.bz2"):
            for p in sorted(root.glob(pat)):
                if not p.is_file():
                    continue
                if p.name.startswith("_dl_"):
                    continue
                key = str(p.resolve())
                if key in seen:
                    continue
                seen.add(key)
                try:
                    st = p.stat()
                except OSError:
                    continue
                mid = hint_match_id_from_filename(p)
                items.append(
                    {
                        "path": key,
                        "name": p.name,
                        "size": st.st_size,
                        "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                        "folder": label,
                        "match_id_hint": mid,
                    }
                )
    items.sort(key=lambda x: x.get("modified") or "", reverse=True)
    return items


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
