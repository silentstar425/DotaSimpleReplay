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

import argparse
import bz2
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import gem
from gem.extractors.players import PlayerExtractor
from gem.parser import ReplayParser


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Dota2 回放增强 GUI</title>
  <style>
    body { margin: 0; background: #0f1116; color: #e8e8e8; font-family: Arial, sans-serif; }
    .app { display: flex; height: 100vh; overflow: hidden; }
    .side {
      width: 300px;
      background: #161b22;
      border-right: 1px solid #2b313a;
      display: flex;
      flex-direction: column;
      padding: 12px;
      box-sizing: border-box;
    }
    .side.right {
      border-right: none;
      border-left: 1px solid #2b313a;
      width: 340px;
    }
    .side h3 { margin: 0 0 12px; font-size: 16px; }
    .center {
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
      padding: 12px;
      box-sizing: border-box;
      gap: 10px;
    }
    .meta { font-size: 13px; color: #c8c8c8; }
    .meta strong { color: #ffffff; }
    .controls { display: flex; align-items: center; gap: 10px; }
    button {
      background: #2d6cdf;
      color: #fff;
      border: none;
      border-radius: 5px;
      padding: 8px 14px;
      cursor: pointer;
    }
    button:hover { background: #3a77e7; }
    input[type=range] { flex: 1; }
    input[type=number], select {
      background: #0f1318;
      color: #e8e8e8;
      border: 1px solid #2f3946;
      border-radius: 4px;
      padding: 6px 8px;
      font-size: 13px;
    }
    #mapCanvas {
      width: 100%;
      height: calc(100vh - 160px);
      min-height: 560px;
      background: #111;
      border: 1px solid #414a56;
      border-radius: 8px;
    }
    .legend { font-size: 12px; color: #9ea7b3; }
    .scroll { overflow: auto; min-height: 0; }
    .board-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      padding: 8px;
      border-bottom: 1px solid #232a33;
      font-size: 13px;
    }
    .board-row .name { color: #f5f5f5; }
    .board-row .val { color: #8fd3ff; font-weight: bold; }
    .status-row {
      display: grid;
      grid-template-columns: 46px 1fr;
      gap: 10px;
      border-bottom: 1px solid #232a33;
      padding: 9px 6px;
      align-items: center;
      font-size: 13px;
    }
    .avatar {
      width: 38px;
      height: 38px;
      border-radius: 999px;
      background: #2f3946;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #fff;
      font-size: 12px;
      position: relative;
      border: 1px solid #606c7d;
    }
    .respawn-badge {
      position: absolute;
      right: -6px;
      bottom: -6px;
      background: #f44336;
      color: #fff;
      border-radius: 999px;
      padding: 1px 5px;
      font-size: 10px;
      border: 1px solid #fff;
      white-space: nowrap;
    }
    .hp { color: #7CFC8C; }
    .mp { color: #7BB5FF; }
    .dead { color: #ff7d7d; }
    .small-muted { color: #9ba7b6; font-size: 12px; }
  </style>
</head>
<body>
  <div class="app">
    <aside class="side left">
      <h3>玩家看板</h3>
      <label class="small-muted" for="boardMetric">排序指标</label>
      <select id="boardMetric">
        <option value="net_worth">资产总额</option>
        <option value="kda">K/D/A</option>
        <option value="lh_dn">正补/反补</option>
        <option value="level">等级</option>
      </select>
      <div style="height: 10px;"></div>
      <div id="boardList" class="scroll"></div>
    </aside>

    <main class="center">
      <div class="meta" id="fileLine"></div>
      <div class="meta" id="tickLine"></div>
      <div class="meta" id="tickRateLine"></div>
      <div class="controls">
        <button id="playBtn">播放</button>
        <input id="slider" type="range" min="0" max="1" step="1" value="0" />
        <label for="fpsInput" class="small-muted">刷新率(FPS)</label>
        <input id="fpsInput" type="number" min="1" max="240" step="1" value="30" style="width: 84px;" />
      </div>
      <canvas id="mapCanvas" width="1200" height="780"></canvas>
      <div class="legend">绿色：天辉（team=2） | 红色：夜魇（team=3） | 死亡英雄不会显示在地图上</div>
    </main>

    <aside class="side right">
      <h3>英雄状态</h3>
      <div class="small-muted" style="margin-bottom: 8px;">显示：HP / MP / 复活倒计时（死亡时）</div>
      <div id="statusList" class="scroll"></div>
    </aside>
  </div>

  <script>
    const shortHeroName = (name) => name.startsWith("npc_dota_hero_")
      ? name.slice("npc_dota_hero_".length)
      : name;

    const heroAvatarText = (name) => {
      const s = shortHeroName(name).replaceAll("_", " ");
      const parts = s.split(" ").filter(Boolean);
      if (parts.length === 0) return "H";
      if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
      return (parts[0][0] + parts[1][0]).toUpperCase();
    };

    const formatGameTime = (seconds) => {
      const sign = seconds < 0 ? "-" : "";
      const absVal = Math.abs(seconds);
      const mm = Math.floor(absVal / 60);
      const ss = absVal - mm * 60;
      return `${sign}${String(mm).padStart(2, "0")}:${ss.toFixed(2).padStart(5, "0")}`;
    };

    const mapToCanvas = (x, y, bounds, canvas) => {
      const pad = 40;
      const nx = bounds.max_x === bounds.min_x ? 0 : (x - bounds.min_x) / (bounds.max_x - bounds.min_x);
      const ny = bounds.max_y === bounds.min_y ? 0 : (y - bounds.min_y) / (bounds.max_y - bounds.min_y);
      const cx = pad + nx * (canvas.width - 2 * pad);
      const cy = pad + (1 - ny) * (canvas.height - 2 * pad);
      return [cx, cy];
    };

    const upperBound = (arr, target) => {
      let left = 0;
      let right = arr.length;
      while (left < right) {
        const mid = (left + right) >> 1;
        if (arr[mid] <= target) left = mid + 1;
        else right = mid;
      }
      return left;
    };

    const stateAtTick = (timeline, tick) => {
      const idx = upperBound(timeline.ticks, tick) - 1;
      if (idx < 0) return null;
      return timeline.states[idx];
    };

    const killsAtTick = (timeline, tick) => upperBound(timeline.kill_event_ticks, tick);

    const deathInfoAtTick = (timeline, tick) => {
      for (const w of timeline.death_windows) {
        const inRange = tick >= w.start_tick && (w.end_tick === null || tick < w.end_tick);
        if (!inRange) continue;
        return {
          is_dead: true,
          remaining_ticks: w.end_tick === null ? null : Math.max(0, w.end_tick - tick),
        };
      }
      return { is_dead: false, remaining_ticks: 0 };
    };

    const defaultState = () => ({
      x: null,
      y: null,
      hp: 0,
      max_hp: 0,
      mana: 0,
      max_mana: 0,
      level: 0,
      net_worth: 0,
      lh: 0,
      dn: 0,
      total_deaths: 0,
    });

    const fileLine = document.getElementById("fileLine");
    const tickLine = document.getElementById("tickLine");
    const tickRateLine = document.getElementById("tickRateLine");
    const playBtn = document.getElementById("playBtn");
    const slider = document.getElementById("slider");
    const boardMetric = document.getElementById("boardMetric");
    const boardList = document.getElementById("boardList");
    const statusList = document.getElementById("statusList");
    const fpsInput = document.getElementById("fpsInput");
    const canvas = document.getElementById("mapCanvas");
    const ctx = canvas.getContext("2d");

    let data = null;
    let playing = false;
    let timer = null;
    let currentTick = 0;

    const renderMap = (tick) => {
      ctx.fillStyle = "#111";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.strokeStyle = "#666";
      ctx.lineWidth = 2;
      ctx.strokeRect(20, 20, canvas.width - 40, canvas.height - 40);
      ctx.fillStyle = "#ccc";
      ctx.font = "14px Arial";
      ctx.fillText("地图（归一化坐标）", 30, 36);

      for (const timeline of data.player_timelines) {
        const st = stateAtTick(timeline, tick);
        if (!st || st.x === null || st.y === null) continue;
        const death = deathInfoAtTick(timeline, tick);
        if (death.is_dead || st.hp <= 0) continue;

        const [cx, cy] = mapToCanvas(st.x, st.y, data.map_bounds, canvas);
        ctx.beginPath();
        ctx.fillStyle = timeline.team === 2 ? "#4CAF50" : "#F44336";
        ctx.strokeStyle = "#ddd";
        ctx.arc(cx, cy, 7, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = "#fff";
        ctx.font = "12px Arial";
        ctx.fillText(shortHeroName(timeline.hero_name), cx + 9, cy - 9);
      }
    };

    const renderBoard = (tick) => {
      const mode = boardMetric.value;
      const rows = [];

      for (const timeline of data.player_timelines) {
        const st = stateAtTick(timeline, tick) || defaultState();
        const kills = killsAtTick(timeline, tick);
        const deaths = st.total_deaths;
        const assists = timeline.final_kda.assists;

        let sortValue = 0;
        let valueText = "";
        if (mode === "net_worth") {
          sortValue = st.net_worth || 0;
          valueText = `${sortValue}`;
        } else if (mode === "kda") {
          sortValue = kills * 100000 - deaths * 100 + assists;
          valueText = `${kills}/${deaths}/${assists}`;
        } else if (mode === "lh_dn") {
          sortValue = (st.lh || 0) * 1000 + (st.dn || 0);
          valueText = `${st.lh || 0}/${st.dn || 0}`;
        } else if (mode === "level") {
          sortValue = st.level || 0;
          valueText = `${sortValue}`;
        }
        rows.push({
          name: timeline.player_name || shortHeroName(timeline.hero_name),
          hero: shortHeroName(timeline.hero_name),
          valueText,
          sortValue,
        });
      }

      rows.sort((a, b) => b.sortValue - a.sortValue || a.hero.localeCompare(b.hero));
      boardList.innerHTML = rows.map((row) => `
        <div class="board-row">
          <div class="name">${row.name}<div class="small-muted">${row.hero}</div></div>
          <div class="val">${row.valueText}</div>
        </div>
      `).join("");
    };

    const renderStatus = (tick) => {
      const sorted = [...data.player_timelines].sort((a, b) => {
        if (a.team !== b.team) return a.team - b.team;
        return a.player_id - b.player_id;
      });

      statusList.innerHTML = sorted.map((timeline) => {
        const st = stateAtTick(timeline, tick) || defaultState();
        const death = deathInfoAtTick(timeline, tick);
        const hpText = `${Math.max(0, Math.round(st.hp || 0))}/${Math.max(0, Math.round(st.max_hp || 0))}`;
        const manaText = `${Math.max(0, Math.round(st.mana || 0))}/${Math.max(0, Math.round(st.max_mana || 0))}`;
        const respawnSec = death.remaining_ticks === null ? "?" : (death.remaining_ticks / data.tick_rate).toFixed(1);
        const respawnBadge = death.is_dead ? `<span class="respawn-badge">${respawnSec}s</span>` : "";
        const deathLine = death.is_dead ? `<div class="dead">死亡中 · 复活剩余 ${respawnSec}s</div>` : "";
        return `
          <div class="status-row">
            <div class="avatar">
              ${heroAvatarText(timeline.hero_name)}
              ${respawnBadge}
            </div>
            <div>
              <div><strong>${timeline.player_name || shortHeroName(timeline.hero_name)}</strong> <span class="small-muted">(${shortHeroName(timeline.hero_name)})</span></div>
              <div class="hp">HP: ${hpText}</div>
              <div class="mp">MP: ${manaText}</div>
              ${deathLine}
            </div>
          </div>
        `;
      }).join("");
    };

    const render = (tick) => {
      tick = Math.max(0, Math.min(data.game_end_tick, tick));
      currentTick = tick;
      slider.value = String(tick);

      const gameSeconds = (tick - data.game_start_tick) / data.tick_rate;
      tickLine.textContent = `Tick: ${tick} | 游戏时间: ${formatGameTime(gameSeconds)} | 游戏开始 tick: ${data.game_start_tick}`;
      renderMap(tick);
      renderBoard(tick);
      renderStatus(tick);
    };

    const stopPlayback = () => {
      playing = false;
      playBtn.textContent = "播放";
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
    };

    const startPlayback = () => {
      const fps = Math.max(1, Number(fpsInput.value) || data.playback_fps || 30);
      fpsInput.value = String(Math.round(fps));
      playing = true;
      playBtn.textContent = "暂停";
      const delay = Math.max(Math.round(1000 / fps), 1);
      timer = setInterval(() => {
        if (currentTick >= data.game_end_tick) {
          stopPlayback();
          return;
        }
        render(currentTick + 1);
      }, delay);
    };

    playBtn.addEventListener("click", () => {
      if (!data) return;
      if (playing) stopPlayback();
      else startPlayback();
    });

    slider.addEventListener("input", (e) => {
      if (!data) return;
      render(Number(e.target.value));
    });

    boardMetric.addEventListener("change", () => {
      if (!data) return;
      renderBoard(currentTick);
    });

    fpsInput.addEventListener("change", () => {
      if (!data) return;
      const fps = Math.max(1, Math.min(240, Number(fpsInput.value) || data.playback_fps || 30));
      fpsInput.value = String(Math.round(fps));
      if (playing) {
        stopPlayback();
        startPlayback();
      }
    });

    (async () => {
      const res = await fetch("/data");
      data = await res.json();
      fileLine.textContent = `文件: ${data.dem_path} | match_id: ${data.match_id}`;
      tickRateLine.textContent = `tick_rate=${data.tick_rate.toFixed(2)}（每秒游戏 tick 数，通常接近 30；它描述游戏模拟频率，不是播放刷新率）`;
      slider.min = "0";
      slider.max = String(data.game_end_tick);
      slider.value = "0";
      fpsInput.value = String(data.playback_fps || 30);
      render(0);
    })();
  </script>
</body>
</html>
"""


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
            "请用: python3 replay_position_gui.py <your.dem|your.dem.bz2>"
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


def _parse_player_snapshots(dem_path: Path) -> tuple[ReplayParser, PlayerExtractor]:
    parser = ReplayParser(str(dem_path))
    player_ext = PlayerExtractor(sample_interval=30, minute_snapshots=False)
    player_ext.attach(parser)
    parser.parse()
    return parser, player_ext


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


def build_gui_payload(replay_path: Path, playback_fps: int) -> dict[str, Any]:
    print(f"[info] 读取回放: {replay_path}")
    dem_path = ensure_dem_path(replay_path)
    print(f"[info] 解析 DEM: {dem_path}")

    match = gem.parse(str(dem_path))
    parser, player_ext = _parse_player_snapshots(dem_path)
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
        state = {
            "x": None if snap.x is None else float(snap.x),
            "y": None if snap.y is None else float(snap.y),
            "hp": int(snap.hp),
            "max_hp": int(snap.max_hp),
            "mana": float(snap.mana),
            "max_mana": float(snap.max_mana),
            "level": int(snap.level),
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
    }
    print(
        f"[info] 回放范围: 0 -> {payload['game_end_tick']} (game_start_tick={payload['game_start_tick']}), "
        f"tick_rate={payload['tick_rate']:.2f}, 玩家轨迹={len(payload['player_timelines'])}"
    )
    return payload


def run_server(host: str, port: int, payload: dict[str, Any], open_browser: bool) -> None:
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    html_bytes = HTML_TEMPLATE.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/" or self.path.startswith("/?"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.end_headers()
                self.wfile.write(html_bytes)
                return
            if self.path == "/data":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload_bytes)))
                self.end_headers()
                self.wfile.write(payload_bytes)
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
    payload = build_gui_payload(replay_path, playback_fps=args.fps)

    if args.export_json:
        out = Path(args.export_json).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[done] 已导出 GUI 数据: {out}")

    if args.no_server:
        return
    run_server(args.host, args.port, payload, open_browser=not args.no_open_browser)


if __name__ == "__main__":
    main()
