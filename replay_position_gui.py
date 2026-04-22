#!/usr/bin/env python3
"""
简易 Dota 2 回放 GUI（浏览器版，初版）

功能：
1) 显示英雄在地图上的位置
2) 按标准速度逐 tick 播放（基于回放推导的 tick_rate）
3) 支持播放/暂停
4) 支持可拖动进度条
"""

from __future__ import annotations

import argparse
import bz2
import json
import webbrowser
from pathlib import Path
from typing import Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gem


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Dota2 回放英雄位置（简易版）</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; background: #0f1116; color: #e6e6e6; }
    .wrap { max-width: 1100px; margin: 0 auto; padding: 14px; }
    .meta { margin-bottom: 8px; font-size: 14px; line-height: 1.5; }
    .controls { display: flex; gap: 12px; align-items: center; margin: 10px 0 14px; }
    button { background: #2d6cdf; color: white; border: none; border-radius: 4px; padding: 8px 14px; cursor: pointer; }
    button:hover { background: #3a77e7; }
    input[type=range] { flex: 1; }
    canvas { width: 100%; height: 760px; background: #111; border: 1px solid #444; border-radius: 6px; }
    .legend { margin-top: 8px; font-size: 13px; color: #b5b5b5; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="meta" id="fileLine"></div>
    <div class="meta" id="tickLine"></div>
    <div class="controls">
      <button id="playBtn">播放</button>
      <input id="slider" type="range" min="0" max="1" step="1" value="0" />
    </div>
    <canvas id="mapCanvas" width="1050" height="760"></canvas>
    <div class="legend">绿色：天辉（team=2） | 红色：夜魇（team=3）</div>
  </div>

  <script>
    const shortHeroName = (name) => name.startsWith("npc_dota_hero_")
      ? name.slice("npc_dota_hero_".length)
      : name;

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

    const stateAtTick = (timeline, tick) => {
      const ticks = timeline.ticks;
      let left = 0;
      let right = ticks.length - 1;
      let ans = -1;
      while (left <= right) {
        const mid = (left + right) >> 1;
        if (ticks[mid] <= tick) {
          ans = mid;
          left = mid + 1;
        } else {
          right = mid - 1;
        }
      }
      return ans < 0 ? null : timeline.states[ans];
    };

    const fileLine = document.getElementById("fileLine");
    const tickLine = document.getElementById("tickLine");
    const playBtn = document.getElementById("playBtn");
    const slider = document.getElementById("slider");
    const canvas = document.getElementById("mapCanvas");
    const ctx = canvas.getContext("2d");

    let data = null;
    let playing = false;
    let timer = null;
    let currentTick = 0;

    const render = (tick) => {
      tick = Math.max(data.game_start_tick, Math.min(data.game_end_tick, tick));
      currentTick = tick;
      slider.value = String(tick);

      const gameSeconds = (tick - data.game_start_tick) / data.tick_rate;
      tickLine.textContent = `Tick: ${tick} | 游戏时间: ${formatGameTime(gameSeconds)} | tick_rate: ${data.tick_rate.toFixed(2)}`;

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
        if (!st) continue;
        const [cx, cy] = mapToCanvas(st.x, st.y, data.map_bounds, canvas);

        ctx.beginPath();
        ctx.fillStyle = st.team === 2 ? "#4CAF50" : "#F44336";
        ctx.strokeStyle = "#ddd";
        ctx.arc(cx, cy, 6, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();

        ctx.fillStyle = "#fff";
        ctx.font = "12px Arial";
        ctx.fillText(shortHeroName(st.hero_name), cx + 9, cy - 8);
      }
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
      playing = true;
      playBtn.textContent = "暂停";
      const delay = Math.max(Math.round(1000 / data.tick_rate), 1);
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

    (async () => {
      const res = await fetch("/data");
      data = await res.json();
      fileLine.textContent = `文件: ${data.dem_path}`;
      slider.min = String(data.game_start_tick);
      slider.max = String(data.game_end_tick);
      slider.value = String(data.game_start_tick);
      render(data.game_start_tick);
    })();
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dota2 回放英雄位置简易 GUI（浏览器版）")
    parser.add_argument(
        "input_replay",
        nargs="?",
        default=None,
        help="回放路径（.dem 或 .dem.bz2）。不传则尝试使用 replay_samples 下第一个回放。",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Web 服务监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="Web 服务端口（默认 8765）")
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="不自动打开浏览器，仅打印访问地址。",
    )
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="只解析并打印摘要，不启动 Web GUI（用于测试）。",
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


def build_gui_payload(replay_path: Path) -> dict[str, Any]:
    print(f"[info] 读取回放: {replay_path}")
    dem_path = ensure_dem_path(replay_path)
    print(f"[info] 解析 DEM: {dem_path}")

    match = gem.parse(str(dem_path))
    dfs = gem.parse_to_dataframe(str(dem_path))
    positions_df = dfs.get("positions")
    if positions_df is None or len(positions_df) == 0:
        raise RuntimeError("解析结果中没有 positions 数据，无法绘制英雄位置。")

    player_timelines: dict[int, dict[str, Any]] = {}
    sorted_positions = positions_df.sort_values(["player_id", "tick"])
    for row in sorted_positions.to_dict(orient="records"):
        player_id = int(row["player_id"])
        bucket = player_timelines.setdefault(
            player_id,
            {
                "player_id": player_id,
                "hero_name": str(row["hero_name"]),
                "team": int(row["team"]),
                "ticks": [],
                "states": [],
            },
        )
        bucket["ticks"].append(int(row["tick"]))
        bucket["states"].append(
            {
                "x": float(row["x"]),
                "y": float(row["y"]),
                "team": int(row["team"]),
                "hero_name": str(row["hero_name"]),
            }
        )

    payload = {
        "dem_path": str(dem_path),
        "match_id": int(match.match_id),
        "game_start_tick": int(match.game_start_tick),
        "game_end_tick": int(match.game_end_tick),
        "tick_rate": compute_tick_rate(match),
        # tick 与游戏时间换算关系：
        # game_time_seconds = (tick - game_start_tick) / tick_rate
        "tick_game_time_relation": "(tick - game_start_tick) / tick_rate",
        "map_bounds": {
            "min_x": float(positions_df["x"].min()),
            "max_x": float(positions_df["x"].max()),
            "min_y": float(positions_df["y"].min()),
            "max_y": float(positions_df["y"].max()),
        },
        "player_timelines": list(player_timelines.values()),
    }
    print(
        f"[info] 回放范围: {payload['game_start_tick']} -> {payload['game_end_tick']}, "
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
    payload = build_gui_payload(replay_path)

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
