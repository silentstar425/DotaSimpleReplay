#!/usr/bin/env python3
"""
增强版 Dota 2 回放 GUI（浏览器版）

能力：
1) 从 tick=0 开始播放（而不是从 game_start_tick）
2) 左侧看板（下拉切换：资产、K/D/A、正补/反补、等级；降序）
3) 右侧英雄状态（血量/蓝量、复活倒计时）
4) 英雄死亡时不在地图上绘制图标
5) 播放刷新率（FPS）默认 30，可调
"""

from __future__ import annotations

import mimetypes
import argparse
import bz2
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from pathlib import Path
from typing import Any

import gem
from gem.extractors.players import PlayerExtractor
from gem.parser import ReplayParser
from replay_cache import cache_path_for_dem, delete_replay_cache, load_replay_cache, save_replay_cache
from replay_world_entities import WorldEntityCollector





def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dota2 回放增强 GUI（浏览器版）")
    parser.add_argument(
        "input_replay",
        nargs="?",
        default=None,
        help="回放路径（.dem 或 .dem.bz2）。不传则尝试使用 replay_samples 下第一个回放。",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Web 服务监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="Web 服务端口（默认 8765）")
    parser.add_argument("--fps", type=int, default=30, help="播放刷新率 FPS（默认 30）")
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="不自动打开浏览器，仅打印访问地址。",
    )
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="只解析并导出数据，不启动 Web GUI（用于测试）。",
    )
    parser.add_argument(
        "--export-json",
        default=None,
        help="可选：将 GUI 使用的数据导出为 JSON 文件。",
    )
    return parser.parse_args()


def resolve_input_path(raw: str | None) -> Path:
    if raw:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"输入回放不存在: {path}")
        return path

    sample_dir = Path("replay_samples").resolve()
    candidates = sorted(sample_dir.glob("*.dem")) + sorted(sample_dir.glob("*.dem.bz2"))
    if not candidates:
        raise FileNotFoundError(
            "未提供 input_replay 且 replay_samples 下找不到回放文件。"
            "请用: python3 run.py <your.dem|your.dem.bz2>"
        )
    return candidates[0]


def ensure_dem_path(input_path: Path) -> Path:
    if input_path.suffix != ".bz2":
        return input_path

    output_dem = input_path.with_suffix("")
    if output_dem.exists():
        return output_dem

    print(f"[info] 解压 .bz2 -> {output_dem}")
    with bz2.open(input_path, "rb") as src, output_dem.open("wb") as dst:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            dst.write(chunk)
    return output_dem


def compute_tick_rate(match: Any) -> float:
    duration_ticks = max(int(match.game_end_tick) - int(match.game_start_tick), 1)
    duration_seconds = float(getattr(match, "duration_seconds", 0.0) or 0.0)
    if duration_seconds > 0:
        return duration_ticks / duration_seconds
    return 30.0


def _print_parse_progress(current_tick: int, total_tick: int, done: bool = False) -> None:
    if total_tick <= 0:
        total_tick = 1
    ratio = max(0.0, min(float(current_tick) / float(total_tick), 1.0))
    width = 36
    filled = int(width * ratio)
    bar = "#" * filled + "-" * (width - filled)
    percent = ratio * 100.0
    sys.stdout.write(f"\r[parse] [{bar}] {percent:6.2f}% ({current_tick}/{total_tick} tick)")
    if done:
        sys.stdout.write("\n")
    sys.stdout.flush()


def _collect_parse_with_progress(
    dem_path: Path,
    estimated_end_tick: int,
) -> tuple[ReplayParser, PlayerExtractor, WorldEntityCollector]:
    parser = ReplayParser(str(dem_path))
    # 使用逐 tick 采样，确保刷新率提升时有足够细粒度的数据可更新。
    player_ext = PlayerExtractor(sample_interval=1, minute_snapshots=False)
    player_ext.attach(parser)
    world_ext = WorldEntityCollector(sample_interval=6)
    world_ext.attach(parser)

    progress_total = max(int(estimated_end_tick), 1)
    last_report_tick = -10**9

    def _progress_callback(_entity: Any, _op: Any) -> None:
        nonlocal last_report_tick
        tick = int(parser.tick)
        if tick - last_report_tick < 180:
            return
        last_report_tick = tick
        _print_parse_progress(tick, progress_total, done=False)

    parser.on_entity(_progress_callback)
    _print_parse_progress(0, progress_total, done=False)
    parser.parse()
    final_tick = min(max(int(parser.tick), 0), progress_total)
    _print_parse_progress(final_tick, progress_total, done=True)
    return parser, player_ext, world_ext


def _xp_to_level(xp: int) -> int:
    # Dota2 英雄等级累计经验阈值（1~30），来自游戏常量。
    # 数组下标表示等级，值表示达到该等级所需的累计经验。
    xp_to_reach_level = [
        0,
        0,
        240,
        640,
        1160,
        1760,
        2440,
        3200,
        4040,
        4960,
        5960,
        7040,
        8200,
        9440,
        10760,
        12160,
        13640,
        15200,
        16840,
        18560,
        20360,
        22240,
        24200,
        26240,
        28360,
        30560,
        32840,
        35200,
        37640,
        40160,
        42760,
    ]
    value = max(int(xp), 0)
    level = 1
    for idx in range(1, len(xp_to_reach_level)):
        if value >= xp_to_reach_level[idx]:
            level = idx
        else:
            break
    return min(level, 30)


def _build_death_windows(ticks: list[int], states: list[dict[str, Any]]) -> list[dict[str, int | None]]:
    windows: list[dict[str, int | None]] = []
    dead_start: int | None = None
    for tick, state in zip(ticks, states, strict=False):
        is_dead = float(state.get("hp", 0.0) or 0.0) <= 0.0
        if dead_start is None and is_dead:
            dead_start = tick
        elif dead_start is not None and not is_dead:
            windows.append({"start_tick": dead_start, "end_tick": tick})
            dead_start = None
    if dead_start is not None:
        windows.append({"start_tick": dead_start, "end_tick": None})
    return windows


def build_gui_payload(replay_path: Path, playback_fps: int) -> tuple[dict[str, Any], Path]:
    print(f"[info] 读取回放: {replay_path}")
    dem_path = ensure_dem_path(replay_path)
    print(f"[info] 解析 DEM: {dem_path}")

    cache_path = cache_path_for_dem(dem_path)
    cached = load_replay_cache(dem_path)
    if cached is not None:
        payload = dict(cached)
        payload["playback_fps"] = int(max(playback_fps, 1))
        payload["cache_enabled"] = True
        payload["cache_hit"] = True
        payload["cache_path"] = str(cache_path)
        print(f"[info] 命中缓存: {cache_path}")
        return payload, dem_path

    match = gem.parse(str(dem_path))
    parser, player_ext, world_ext = _collect_parse_with_progress(
        dem_path,
        estimated_end_tick=max(int(match.game_end_tick), 1),
    )
    tick_rate = compute_tick_rate(match)

    player_timelines: dict[int, dict[str, Any]] = {}
    hero_to_pid: dict[str, int] = {}
    for pp in match.players:
        pid = int(pp.player_id)
        hero = str(pp.hero_name)
        player_timelines[pid] = {
            "player_id": pid,
            "player_name": str(pp.player_name or ""),
            "hero_name": hero,
            "team": int(pp.team),
            "final_kda": {
                "kills": int(pp.kills),
                "deaths": int(pp.deaths),
                "assists": int(pp.assists),
            },
            "kill_event_ticks": [],
            "ticks": [],
            "states": [],
            "death_windows": [],
        }
        if hero:
            hero_to_pid[hero.lower()] = pid

    for entry in match.combat_log:
        if (
            entry.log_type == "DEATH"
            and entry.attacker_is_hero
            and entry.target_is_hero
            and entry.attacker_name
        ):
            pid = hero_to_pid.get(entry.attacker_name.lower())
            if pid is not None:
                player_timelines[pid]["kill_event_ticks"].append(int(entry.tick))

    min_x = float("inf")
    max_x = float("-inf")
    min_y = float("inf")
    max_y = float("-inf")

    for snap in player_ext.snapshots:
        pid = int(snap.player_id)
        if pid not in player_timelines:
            continue
        resolved_level = _xp_to_level(int(snap.xp))
        state = {
            "x": None if snap.x is None else float(snap.x),
            "y": None if snap.y is None else float(snap.y),
            "hp": int(snap.hp),
            "max_hp": int(snap.max_hp),
            "mana": float(snap.mana),
            "max_mana": float(snap.max_mana),
            "level": int(resolved_level),
            "net_worth": int(snap.net_worth),
            "lh": int(snap.lh),
            "dn": int(snap.dn),
            "total_deaths": int(snap.total_deaths),
        }
        player_timelines[pid]["ticks"].append(int(snap.tick))
        player_timelines[pid]["states"].append(state)
        if state["x"] is not None and state["y"] is not None:
            min_x = min(min_x, state["x"])
            max_x = max(max_x, state["x"])
            min_y = min(min_y, state["y"])
            max_y = max(max_y, state["y"])

    entity_timelines = world_ext.to_payload()
    for row in entity_timelines:
        for st in row.get("states", []):
            x = st.get("x")
            y = st.get("y")
            if x is None or y is None:
                continue
            min_x = min(min_x, float(x))
            max_x = max(max_x, float(x))
            min_y = min(min_y, float(y))
            max_y = max(max_y, float(y))

    for timeline in player_timelines.values():
        timeline["kill_event_ticks"] = sorted(int(x) for x in timeline["kill_event_ticks"])
        timeline["death_windows"] = _build_death_windows(timeline["ticks"], timeline["states"])

    if min_x == float("inf"):
        min_x, max_x, min_y, max_y = 0.0, 1.0, 0.0, 1.0

    game_end_tick = max(int(match.game_end_tick), int(parser.tick))
    payload = {
        "dem_path": str(dem_path),
        "match_id": int(match.match_id),
        "game_start_tick": int(match.game_start_tick),
        "game_end_tick": game_end_tick,
        "tick_rate": tick_rate,
        "playback_fps": int(max(playback_fps, 1)),
        # tick 与游戏时间换算关系：
        # game_time_seconds = (tick - game_start_tick) / tick_rate
        "tick_game_time_relation": "(tick - game_start_tick) / tick_rate",
        "map_bounds": {
            "min_x": float(min_x),
            "max_x": float(max_x),
            "min_y": float(min_y),
            "max_y": float(max_y),
        },
        "player_timelines": [player_timelines[k] for k in sorted(player_timelines.keys())],
        "entity_timelines": entity_timelines,
    }
    save_replay_cache(dem_path, payload)
    payload["cache_enabled"] = True
    payload["cache_hit"] = False
    payload["cache_path"] = str(cache_path)
    print(f"[info] 已写入缓存: {cache_path}")
    print(
        f"[info] 回放范围: 0 -> {payload['game_end_tick']} (game_start_tick={payload['game_start_tick']}), "
        f"tick_rate={payload['tick_rate']:.2f}, 玩家轨迹={len(payload['player_timelines'])}, "
        f"世界实体轨迹={len(payload['entity_timelines'])}"
    )
    return payload, dem_path


def _safe_replay_id_from_name(name: str) -> str:
    match = re.search(r"(\d{6,})", name)
    if match:
        return match.group(1)
    return name


def list_replay_records(replay_dir: Path) -> list[dict[str, Any]]:
    replay_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(
        list(replay_dir.glob("*.dem")) + list(replay_dir.glob("*.dem.bz2")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    rows: list[dict[str, Any]] = []
    for path in files:
        downloaded_at = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
        parsed = False
        parse_error = ""
        try:
            dem_path = ensure_dem_path(path)
            parsed = load_replay_cache(dem_path) is not None
        except Exception as exc:
            parse_error = str(exc)
        rows.append(
            {
                "replay_id": _safe_replay_id_from_name(path.stem),
                "file_name": path.name,
                "file_path": str(path.resolve()),
                "dem_path": str(path.resolve()),
                "downloaded_at": downloaded_at,
                "parsed": parsed,
                "parse_error": parse_error,
            }
        )
    return rows


def download_replay_by_id(replay_id: str, replay_dir: Path) -> Path:
    rid = replay_id.strip()
    if not re.fullmatch(r"\d{6,}", rid):
        raise ValueError("录像ID必须是至少6位数字。")
    replay_dir.mkdir(parents=True, exist_ok=True)
    output_path = replay_dir / f"match_{rid}.dem.bz2"
    url = f"https://api.opendota.com/api/replays?match_id={rid}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("未查询到可下载录像信息。")
    item = payload[0]
    cluster = item.get("cluster")
    replay_salt = item.get("replay_salt")
    if cluster is None or replay_salt is None:
        raise RuntimeError("查询结果缺少 cluster 或 replay_salt。")
    replay_url = f"https://replay{int(cluster)}.valve.net/570/{rid}_{int(replay_salt)}.dem.bz2"
    req = urllib.request.Request(replay_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, output_path.open("wb") as f:
        f.write(resp.read())
    if output_path.stat().st_size <= 0:
        raise RuntimeError("下载完成但文件为空。")
    return output_path


def run_server(host: str, port: int, payload: dict[str, Any], dem_path: Path, open_browser: bool) -> None:
    def current_payload_bytes() -> bytes:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    web_dist = Path(__file__).resolve().parent / "web" / "dist"
    replay_dir = Path("replay_samples").resolve()
    nonlocal_dem_path_holder: dict[str, Path] = {"path": dem_path}

    map_bg_path = Path(__file__).resolve().parent / "assets" / "maps" / "map_full.png"
    map_bg_bytes = map_bg_path.read_bytes() if map_bg_path.exists() else None
    print(
        f"[info] React 静态资源目录: {web_dist} (exists={web_dist.is_dir()}) | "
        f"地图底图: {map_bg_path} (exists={map_bg_path.exists()})"
    )

    def read_index_html() -> bytes:
        index_path = web_dist / "index.html"
        if not index_path.is_file():
            raise FileNotFoundError(
                f"缺少前端构建产物 {index_path}。请在仓库根目录执行: cd web && npm install && npm run build"
            )
        return index_path.read_bytes()

    def safe_dist_asset(rel: str) -> Path | None:
        """返回 web_dist 下的安全文件路径，否则 None。"""
        rel = rel.lstrip("/")
        if not rel or ".." in Path(rel).parts:
            return None
        root = web_dist.resolve()
        candidate = (web_dist / rel).resolve()
        if not candidate.is_relative_to(root):
            return None
        return candidate if candidate.is_file() else None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path_only = urllib.parse.urlparse(self.path).path

            if path_only == "/" or path_only == "":
                try:
                    html_bytes = read_index_html()
                except FileNotFoundError as exc:
                    msg = str(exc).encode("utf-8")
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.end_headers()
                self.wfile.write(html_bytes)
                return

            if path_only == "/data":
                payload_bytes = current_payload_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload_bytes)))
                self.end_headers()
                self.wfile.write(payload_bytes)
                return

            if path_only == "/replays":
                body = json.dumps({"replays": list_replay_records(replay_dir)}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path_only == "/assets/maps/map_full.png":
                if map_bg_bytes is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(map_bg_bytes)))
                self.end_headers()
                self.wfile.write(map_bg_bytes)
                return

            if path_only.startswith("/assets/"):
                rel_from_dist = path_only[1:]
                static_path = safe_dist_asset(rel_from_dist)
                if static_path is not None:
                    body = static_path.read_bytes()
                    mime, _ = mimetypes.guess_type(str(static_path))
                    if mime is None:
                        mime = "application/octet-stream"
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            if self.path in ("/parse_replay", "/load_replay", "/download_replay_by_id"):
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    req_data = json.loads(raw.decode("utf-8"))
                except Exception:
                    req_data = {}

                def _json_response(code: int, obj: dict[str, Any]) -> None:
                    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                if self.path == "/download_replay_by_id":
                    try:
                        replay_id = str(req_data.get("replay_id", "")).strip()
                        file_path = download_replay_by_id(replay_id, replay_dir)
                        _json_response(200, {"ok": True, "file_path": str(file_path.resolve())})
                    except Exception as exc:
                        _json_response(400, {"ok": False, "error": str(exc)})
                    return

                dem_path_raw = str(req_data.get("dem_path", "")).strip()
                if not dem_path_raw:
                    _json_response(400, {"ok": False, "error": "dem_path 不能为空。"})
                    return
                dem_candidate = Path(dem_path_raw).expanduser().resolve()
                if not dem_candidate.exists():
                    _json_response(404, {"ok": False, "error": f"录像文件不存在: {dem_candidate}"})
                    return
                try:
                    dem_ready = ensure_dem_path(dem_candidate)
                except Exception as exc:
                    _json_response(400, {"ok": False, "error": str(exc)})
                    return

                if self.path == "/parse_replay":
                    try:
                        build_gui_payload(dem_ready, playback_fps=int(payload.get("playback_fps", 30)))
                        _json_response(200, {"ok": True})
                    except Exception as exc:
                        _json_response(400, {"ok": False, "error": str(exc)})
                    return

                if self.path == "/load_replay":
                    try:
                        new_payload, new_dem_path = build_gui_payload(
                            dem_ready,
                            playback_fps=int(payload.get("playback_fps", 30)),
                        )
                        payload.clear()
                        payload.update(new_payload)
                        nonlocal_dem_path_holder["path"] = new_dem_path
                        _json_response(200, {"ok": True, "payload": payload})
                    except Exception as exc:
                        _json_response(400, {"ok": False, "error": str(exc)})
                    return

            if self.path == "/clear_cache":
                target_dem_path = nonlocal_dem_path_holder["path"]
                deleted = delete_replay_cache(target_dem_path)
                if deleted:
                    payload["cache_hit"] = False
                body = json.dumps(
                    {
                        "deleted": bool(deleted),
                        "cache_path": str(cache_path_for_dem(target_dem_path)),
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format_str: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"[done] GUI 地址: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    args = parse_args()
    replay_path = resolve_input_path(args.input_replay)
    payload, dem_path = build_gui_payload(replay_path, playback_fps=args.fps)

    if args.export_json:
        out = Path(args.export_json).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[done] 已导出 GUI 数据: {out}")

    if args.no_server:
        return
    run_server(args.host, args.port, payload, dem_path, open_browser=not args.no_open_browser)


if __name__ == "__main__":
    main()
