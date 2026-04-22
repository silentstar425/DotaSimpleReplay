#!/usr/bin/env python3
"""
简易 Dota 2 回放 GUI（Tkinter 版，初版）

功能：
1) 显示英雄在地图上的位置
2) 按标准速度逐 tick 播放（基于回放推导的 tick_rate）
3) 支持播放/暂停
4) 支持可拖动进度条
"""

from __future__ import annotations

import argparse
import bisect
import bz2
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import tkinter as tk

import gem


@dataclass
class HeroState:
    player_id: int
    hero_name: str
    team: int
    x: float
    y: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dota2 回放英雄位置简易 GUI（Tkinter 版）")
    parser.add_argument(
        "input_replay",
        nargs="?",
        default=None,
        help="回放路径（.dem 或 .dem.bz2）。不传则尝试使用 replay_samples 下第一个回放。",
    )
    parser.add_argument("--width", type=int, default=1000, help="窗口宽度（默认 1000）")
    parser.add_argument("--height", type=int, default=980, help="窗口高度（默认 980）")
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
            "请用: python3 replay_position_gui_tk.py <your.dem|your.dem.bz2>"
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


def tick_to_game_time_seconds(tick: int, game_start_tick: int, tick_rate: float) -> float:
    # tick 与游戏时间换算关系：
    # game_time_seconds = (tick - game_start_tick) / tick_rate
    return (tick - game_start_tick) / tick_rate


def format_game_time(seconds: float) -> str:
    sign = "-" if seconds < 0 else ""
    seconds_abs = abs(seconds)
    mm = int(seconds_abs // 60)
    ss = seconds_abs - mm * 60
    return f"{sign}{mm:02d}:{ss:05.2f}"


def short_hero_name(hero_name: str) -> str:
    prefix = "npc_dota_hero_"
    if hero_name.startswith(prefix):
        return hero_name[len(prefix) :]
    return hero_name


class ReplayPositionTkGUI:
    def __init__(self, replay_path: Path, width: int, height: int) -> None:
        self.replay_path = replay_path
        self.window_width = width
        self.window_height = height

        self.tick_rate = 30.0
        self.game_start_tick = 0
        self.game_end_tick = 0
        self.current_tick = 0

        # player_id -> {"ticks": [...], "states": [HeroState, ...]}
        self.player_timelines: dict[int, dict[str, Any]] = {}

        self.min_x = 0.0
        self.max_x = 1.0
        self.min_y = 0.0
        self.max_y = 1.0

        self.playing = False
        self.after_id: str | None = None
        self._suppress_scale_callback = False

        self.root = tk.Tk()
        self.root.title("Dota2 回放英雄位置（Tkinter 简易版）")
        self.root.geometry(f"{self.window_width}x{self.window_height}")

        self._build_ui()
        self._load_replay()
        self._render_tick(self.current_tick)

    def _build_ui(self) -> None:
        top_frame = tk.Frame(self.root)
        top_frame.pack(fill=tk.X, padx=8, pady=6)

        self.file_label = tk.Label(top_frame, text="文件: -", anchor="w")
        self.file_label.pack(fill=tk.X)

        self.tick_label = tk.Label(top_frame, text="Tick: -", anchor="w")
        self.tick_label.pack(fill=tk.X)

        canvas_height = max(self.window_height - 190, 200)
        self.canvas = tk.Canvas(
            self.root,
            width=self.window_width - 20,
            height=canvas_height,
            bg="#111111",
            highlightthickness=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        control_frame = tk.Frame(self.root)
        control_frame.pack(fill=tk.X, padx=8, pady=(0, 8))

        self.play_btn = tk.Button(control_frame, text="播放", width=10, command=self.toggle_play)
        self.play_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.scale = tk.Scale(
            control_frame,
            from_=0,
            to=1,
            orient=tk.HORIZONTAL,
            showvalue=True,
            resolution=1,
            command=self.on_seek,
        )
        self.scale.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _load_replay(self) -> None:
        print(f"[info] 读取回放: {self.replay_path}")
        dem_path = ensure_dem_path(self.replay_path)
        print(f"[info] 解析 DEM: {dem_path}")

        match = gem.parse(str(dem_path))
        dfs = gem.parse_to_dataframe(str(dem_path))
        positions_df = dfs.get("positions")
        if positions_df is None or len(positions_df) == 0:
            raise RuntimeError("解析结果中没有 positions 数据，无法绘制英雄位置。")

        self.tick_rate = compute_tick_rate(match)
        self.game_start_tick = int(match.game_start_tick)
        self.game_end_tick = int(match.game_end_tick)
        self.current_tick = self.game_start_tick

        self.min_x = float(positions_df["x"].min())
        self.max_x = float(positions_df["x"].max())
        self.min_y = float(positions_df["y"].min())
        self.max_y = float(positions_df["y"].max())

        sorted_positions = positions_df.sort_values(["player_id", "tick"])
        for row in sorted_positions.to_dict(orient="records"):
            player_id = int(row["player_id"])
            state = HeroState(
                player_id=player_id,
                hero_name=str(row["hero_name"]),
                team=int(row["team"]),
                x=float(row["x"]),
                y=float(row["y"]),
            )
            bucket = self.player_timelines.setdefault(player_id, {"ticks": [], "states": []})
            bucket["ticks"].append(int(row["tick"]))
            bucket["states"].append(state)

        self.file_label.config(text=f"文件: {dem_path}")
        self.scale.config(from_=self.game_start_tick, to=self.game_end_tick)
        self._set_scale_value(self.current_tick)

        print(
            f"[info] 回放范围: {self.game_start_tick} -> {self.game_end_tick}, "
            f"tick_rate={self.tick_rate:.2f}, 玩家轨迹={len(self.player_timelines)}"
        )

    def _set_scale_value(self, tick: int) -> None:
        self._suppress_scale_callback = True
        self.scale.set(tick)
        self._suppress_scale_callback = False

    def _map_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        width = max(self.canvas.winfo_width(), 100)
        height = max(self.canvas.winfo_height(), 100)
        pad = 40
        nx = 0.0 if self.max_x == self.min_x else (x - self.min_x) / (self.max_x - self.min_x)
        ny = 0.0 if self.max_y == self.min_y else (y - self.min_y) / (self.max_y - self.min_y)
        cx = pad + nx * (width - 2 * pad)
        cy = pad + (1.0 - ny) * (height - 2 * pad)
        return cx, cy

    def _hero_states_at_tick(self, tick: int) -> list[HeroState]:
        out: list[HeroState] = []
        for timeline in self.player_timelines.values():
            ticks = timeline["ticks"]
            idx = bisect.bisect_right(ticks, tick) - 1
            if idx < 0:
                continue
            out.append(timeline["states"][idx])
        return out

    def _render_tick(self, tick: int) -> None:
        tick = max(self.game_start_tick, min(self.game_end_tick, int(tick)))
        self.current_tick = tick

        game_seconds = tick_to_game_time_seconds(tick, self.game_start_tick, self.tick_rate)
        self.tick_label.config(
            text=f"Tick: {tick} | 游戏时间: {format_game_time(game_seconds)} | tick_rate: {self.tick_rate:.2f}"
        )

        self.canvas.delete("all")
        width = max(self.canvas.winfo_width(), 100)
        height = max(self.canvas.winfo_height(), 100)

        self.canvas.create_rectangle(20, 20, width - 20, height - 20, outline="#555", width=2)
        self.canvas.create_text(28, 12, text="地图（归一化坐标）", fill="#ccc", anchor="w")

        for state in self._hero_states_at_tick(tick):
            color = "#4CAF50" if state.team == 2 else "#F44336"
            cx, cy = self._map_to_canvas(state.x, state.y)
            r = 7
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="#ddd")
            self.canvas.create_text(
                cx + 10,
                cy - 10,
                text=short_hero_name(state.hero_name),
                fill="#fff",
                anchor="w",
                font=("Arial", 9),
            )

        self._set_scale_value(tick)

    def on_seek(self, value: str) -> None:
        if self._suppress_scale_callback:
            return
        self._render_tick(int(float(value)))

    def toggle_play(self) -> None:
        self.playing = not self.playing
        self.play_btn.config(text="暂停" if self.playing else "播放")
        if self.playing:
            self._schedule_next_tick()
        elif self.after_id is not None:
            self.root.after_cancel(self.after_id)
            self.after_id = None

    def _schedule_next_tick(self) -> None:
        if not self.playing:
            return

        next_tick = self.current_tick + 1
        if next_tick > self.game_end_tick:
            self.playing = False
            self.play_btn.config(text="播放")
            self.after_id = None
            return

        self._render_tick(next_tick)
        delay_ms = max(int(round(1000.0 / self.tick_rate)), 1)
        self.after_id = self.root.after(delay_ms, self._schedule_next_tick)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    args = parse_args()
    replay_path = resolve_input_path(args.input_replay)
    app = ReplayPositionTkGUI(replay_path=replay_path, width=args.width, height=args.height)
    app.run()


if __name__ == "__main__":
    main()
