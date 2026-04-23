#!/usr/bin/env python3
"""
增强版 Dota 2 回放 GUI（Tkinter 版）

能力：
1) 从 tick=0 开始播放（而不是从 game_start_tick）
2) 左侧看板（下拉切换：资产、K/D/A、正补/反补、等级；降序）
3) 右侧英雄状态（血量/蓝量、复活倒计时）
4) 英雄死亡时不在地图上绘制图标
5) 播放刷新率（FPS）默认 30，可调
"""

from __future__ import annotations

import argparse
import bisect
import bz2
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import tkinter as tk
from tkinter import ttk

import gem
from gem.extractors.players import PlayerExtractor
from gem.parser import ReplayParser


@dataclass
class HeroState:
    x: float | None
    y: float | None
    hp: int
    max_hp: int
    mana: float
    max_mana: float
    level: int
    net_worth: int
    lh: int
    dn: int
    total_deaths: int


@dataclass
class PlayerTimeline:
    player_id: int
    player_name: str
    hero_name: str
    team: int
    final_kills: int
    final_deaths: int
    final_assists: int
    kill_event_ticks: list[int] = field(default_factory=list)
    ticks: list[int] = field(default_factory=list)
    states: list[HeroState] = field(default_factory=list)
    death_windows: list[dict[str, int | None]] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dota2 回放增强 GUI（Tkinter 版）")
    parser.add_argument(
        "input_replay",
        nargs="?",
        default=None,
        help="回放路径（.dem 或 .dem.bz2）。不传则尝试使用 replay_samples 下第一个回放。",
    )
    parser.add_argument("--width", type=int, default=1500, help="窗口宽度（默认 1500）")
    parser.add_argument("--height", type=int, default=980, help="窗口高度（默认 980）")
    parser.add_argument("--fps", type=int, default=30, help="默认播放刷新率 FPS（默认 30）")
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
    return hero_name[len(prefix) :] if hero_name.startswith(prefix) else hero_name


def avatar_text(hero_name: str) -> str:
    s = short_hero_name(hero_name).replace("_", " ")
    parts = [x for x in s.split(" ") if x]
    if not parts:
        return "H"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def build_death_windows(ticks: list[int], states: list[HeroState]) -> list[dict[str, int | None]]:
    windows: list[dict[str, int | None]] = []
    start: int | None = None
    for tick, state in zip(ticks, states, strict=False):
        dead = state.hp <= 0
        if start is None and dead:
            start = tick
        elif start is not None and not dead:
            windows.append({"start_tick": start, "end_tick": tick})
            start = None
    if start is not None:
        windows.append({"start_tick": start, "end_tick": None})
    return windows


class ReplayPositionTkGUI:
    BOARD_METRICS = ("资产总额", "K/D/A", "正补/反补", "等级")

    def __init__(self, replay_path: Path, width: int, height: int, default_fps: int) -> None:
        self.replay_path = replay_path
        self.window_width = width
        self.window_height = height
        self.default_fps = max(1, default_fps)

        self.tick_rate = 30.0
        self.game_start_tick = 0
        self.game_end_tick = 0
        self.current_tick = 0

        self.min_x = 0.0
        self.max_x = 1.0
        self.min_y = 0.0
        self.max_y = 1.0
        self.timelines: list[PlayerTimeline] = []

        self.playing = False
        self.after_id: str | None = None
        self._suppress_scale_callback = False

        self.root = tk.Tk()
        self.root.title("Dota2 回放增强 GUI（Tkinter 版）")
        self.root.geometry(f"{self.window_width}x{self.window_height}")

        self._build_ui()
        self._load_replay()
        self._render_tick(0)

    def _build_ui(self) -> None:
        container = tk.Frame(self.root, bg="#0f1116")
        container.pack(fill=tk.BOTH, expand=True)

        self.left_panel = tk.Frame(container, bg="#161b22", width=290)
        self.left_panel.pack(side=tk.LEFT, fill=tk.Y)
        self.left_panel.pack_propagate(False)

        self.center_panel = tk.Frame(container, bg="#0f1116")
        self.center_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.right_panel = tk.Frame(container, bg="#161b22", width=340)
        self.right_panel.pack(side=tk.LEFT, fill=tk.Y)
        self.right_panel.pack_propagate(False)

        self._build_left_panel()
        self._build_center_panel()
        self._build_right_panel()

    def _build_left_panel(self) -> None:
        tk.Label(
            self.left_panel,
            text="玩家看板",
            fg="#e8e8e8",
            bg="#161b22",
            font=("Arial", 13, "bold"),
            anchor="w",
        ).pack(fill=tk.X, padx=10, pady=(10, 6))

        tk.Label(
            self.left_panel,
            text="排序指标",
            fg="#a8b2bf",
            bg="#161b22",
            font=("Arial", 10),
            anchor="w",
        ).pack(fill=tk.X, padx=10, pady=(0, 4))

        self.metric_var = tk.StringVar(value=self.BOARD_METRICS[0])
        self.metric_combo = ttk.Combobox(
            self.left_panel,
            values=self.BOARD_METRICS,
            textvariable=self.metric_var,
            state="readonly",
        )
        self.metric_combo.pack(fill=tk.X, padx=10, pady=(0, 8))
        self.metric_combo.bind("<<ComboboxSelected>>", lambda _e: self._render_board(self.current_tick))

        self.board_list = tk.Listbox(
            self.left_panel,
            bg="#10151b",
            fg="#d9e1ea",
            selectbackground="#2d6cdf",
            borderwidth=0,
            highlightthickness=0,
            font=("Consolas", 11),
        )
        self.board_list.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

    def _build_center_panel(self) -> None:
        top = tk.Frame(self.center_panel, bg="#0f1116")
        top.pack(fill=tk.X, padx=10, pady=8)

        self.file_label = tk.Label(top, text="文件: -", fg="#d7d7d7", bg="#0f1116", anchor="w")
        self.file_label.pack(fill=tk.X)
        self.tick_label = tk.Label(top, text="Tick: -", fg="#f2f2f2", bg="#0f1116", anchor="w")
        self.tick_label.pack(fill=tk.X)
        self.tickrate_label = tk.Label(top, text="tick_rate: -", fg="#9db3c8", bg="#0f1116", anchor="w")
        self.tickrate_label.pack(fill=tk.X)

        controls = tk.Frame(self.center_panel, bg="#0f1116")
        controls.pack(fill=tk.X, padx=10, pady=(0, 8))

        self.play_btn = tk.Button(
            controls,
            text="播放",
            width=10,
            bg="#2d6cdf",
            fg="#ffffff",
            relief=tk.FLAT,
            command=self.toggle_play,
        )
        self.play_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.scale = tk.Scale(
            controls,
            from_=0,
            to=1,
            orient=tk.HORIZONTAL,
            showvalue=True,
            resolution=1,
            command=self.on_seek,
            troughcolor="#26313d",
            fg="#e8e8e8",
            bg="#0f1116",
            highlightthickness=0,
        )
        self.scale.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Label(controls, text="刷新率(FPS)", fg="#9fb0c1", bg="#0f1116").pack(side=tk.LEFT, padx=(10, 6))
        self.fps_var = tk.StringVar(value=str(self.default_fps))
        self.fps_spin = tk.Spinbox(
            controls,
            from_=1,
            to=240,
            increment=1,
            width=6,
            textvariable=self.fps_var,
            command=self._on_fps_change,
            bg="#11161d",
            fg="#e8e8e8",
            relief=tk.FLAT,
        )
        self.fps_spin.pack(side=tk.LEFT)

        self.canvas = tk.Canvas(
            self.center_panel,
            bg="#111111",
            highlightthickness=1,
            highlightbackground="#414a56",
        )
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))

        tk.Label(
            self.center_panel,
            text="绿色：天辉（team=2） | 红色：夜魇（team=3） | 死亡英雄不会显示在地图上",
            fg="#9ea7b3",
            bg="#0f1116",
            anchor="w",
        ).pack(fill=tk.X, padx=10, pady=(0, 10))

    def _build_right_panel(self) -> None:
        tk.Label(
            self.right_panel,
            text="英雄状态",
            fg="#e8e8e8",
            bg="#161b22",
            font=("Arial", 13, "bold"),
            anchor="w",
        ).pack(fill=tk.X, padx=10, pady=(10, 4))

        tk.Label(
            self.right_panel,
            text="显示：HP / MP / 复活倒计时（死亡时）",
            fg="#a8b2bf",
            bg="#161b22",
            anchor="w",
        ).pack(fill=tk.X, padx=10, pady=(0, 8))

        self.status_canvas = tk.Canvas(
            self.right_panel,
            bg="#10151b",
            highlightthickness=0,
        )
        self.status_canvas.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

    def _load_replay(self) -> None:
        print(f"[info] 读取回放: {self.replay_path}")
        dem_path = ensure_dem_path(self.replay_path)
        print(f"[info] 解析 DEM: {dem_path}")

        match = gem.parse(str(dem_path))
        parser = ReplayParser(str(dem_path))
        player_ext = PlayerExtractor(sample_interval=30, minute_snapshots=False)
        player_ext.attach(parser)
        parser.parse()

        self.tick_rate = compute_tick_rate(match)
        self.game_start_tick = int(match.game_start_tick)
        self.game_end_tick = max(int(match.game_end_tick), int(parser.tick))
        self.current_tick = 0

        by_pid: dict[int, PlayerTimeline] = {}
        hero_to_pid: dict[str, int] = {}
        for pp in match.players:
            pid = int(pp.player_id)
            tl = PlayerTimeline(
                player_id=pid,
                player_name=str(pp.player_name or ""),
                hero_name=str(pp.hero_name),
                team=int(pp.team),
                final_kills=int(pp.kills),
                final_deaths=int(pp.deaths),
                final_assists=int(pp.assists),
            )
            by_pid[pid] = tl
            if tl.hero_name:
                hero_to_pid[tl.hero_name.lower()] = pid

        for entry in match.combat_log:
            if (
                entry.log_type == "DEATH"
                and entry.attacker_is_hero
                and entry.target_is_hero
                and entry.attacker_name
            ):
                pid = hero_to_pid.get(entry.attacker_name.lower())
                if pid is not None and pid in by_pid:
                    by_pid[pid].kill_event_ticks.append(int(entry.tick))

        self.min_x = float("inf")
        self.max_x = float("-inf")
        self.min_y = float("inf")
        self.max_y = float("-inf")
        for snap in player_ext.snapshots:
            pid = int(snap.player_id)
            tl = by_pid.get(pid)
            if tl is None:
                continue
            state = HeroState(
                x=None if snap.x is None else float(snap.x),
                y=None if snap.y is None else float(snap.y),
                hp=int(snap.hp),
                max_hp=int(snap.max_hp),
                mana=float(snap.mana),
                max_mana=float(snap.max_mana),
                level=int(snap.level),
                net_worth=int(snap.net_worth),
                lh=int(snap.lh),
                dn=int(snap.dn),
                total_deaths=int(snap.total_deaths),
            )
            tl.ticks.append(int(snap.tick))
            tl.states.append(state)
            if state.x is not None and state.y is not None:
                self.min_x = min(self.min_x, state.x)
                self.max_x = max(self.max_x, state.x)
                self.min_y = min(self.min_y, state.y)
                self.max_y = max(self.max_y, state.y)

        if self.min_x == float("inf"):
            self.min_x, self.max_x, self.min_y, self.max_y = 0.0, 1.0, 0.0, 1.0

        self.timelines = [by_pid[k] for k in sorted(by_pid.keys())]
        for tl in self.timelines:
            tl.kill_event_ticks.sort()
            tl.death_windows = build_death_windows(tl.ticks, tl.states)

        self.file_label.config(text=f"文件: {dem_path} | match_id: {int(match.match_id)}")
        self.tickrate_label.config(
            text=(
                f"tick_rate={self.tick_rate:.2f}（每秒游戏 tick 数，通常接近 30；"
                "它是游戏模拟频率，不是播放刷新率）"
            )
        )
        self.scale.config(from_=0, to=self.game_end_tick)
        self._set_scale_value(0)

        print(
            f"[info] 回放范围: 0 -> {self.game_end_tick} (game_start_tick={self.game_start_tick}), "
            f"tick_rate={self.tick_rate:.2f}, 玩家轨迹={len(self.timelines)}"
        )

    def _set_scale_value(self, tick: int) -> None:
        self._suppress_scale_callback = True
        self.scale.set(tick)
        self._suppress_scale_callback = False

    def _map_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        width = max(self.canvas.winfo_width(), 200)
        height = max(self.canvas.winfo_height(), 200)
        pad = 40
        nx = 0.0 if self.max_x == self.min_x else (x - self.min_x) / (self.max_x - self.min_x)
        ny = 0.0 if self.max_y == self.min_y else (y - self.min_y) / (self.max_y - self.min_y)
        cx = pad + nx * (width - 2 * pad)
        cy = pad + (1.0 - ny) * (height - 2 * pad)
        return cx, cy

    def _state_at_tick(self, tl: PlayerTimeline, tick: int) -> HeroState | None:
        idx = bisect.bisect_right(tl.ticks, tick) - 1
        if idx < 0:
            return None
        return tl.states[idx]

    def _kills_at_tick(self, tl: PlayerTimeline, tick: int) -> int:
        return bisect.bisect_right(tl.kill_event_ticks, tick)

    def _death_info_at_tick(self, tl: PlayerTimeline, tick: int) -> tuple[bool, int | None]:
        for window in tl.death_windows:
            start_tick = int(window["start_tick"])
            end_tick = window["end_tick"]
            if tick < start_tick:
                continue
            if end_tick is None or tick < int(end_tick):
                if end_tick is None:
                    return True, None
                return True, max(int(end_tick) - tick, 0)
        return False, 0

    def _render_map(self, tick: int) -> None:
        self.canvas.delete("all")
        width = max(self.canvas.winfo_width(), 200)
        height = max(self.canvas.winfo_height(), 200)
        self.canvas.create_rectangle(20, 20, width - 20, height - 20, outline="#666", width=2)
        self.canvas.create_text(28, 12, text="地图（归一化坐标）", fill="#ccc", anchor="w")

        for tl in self.timelines:
            st = self._state_at_tick(tl, tick)
            if st is None or st.x is None or st.y is None:
                continue
            is_dead, _remain = self._death_info_at_tick(tl, tick)
            if is_dead or st.hp <= 0:
                continue
            color = "#4CAF50" if tl.team == 2 else "#F44336"
            cx, cy = self._map_to_canvas(st.x, st.y)
            r = 7
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="#ddd")
            self.canvas.create_text(
                cx + 10,
                cy - 10,
                text=short_hero_name(tl.hero_name),
                fill="#fff",
                anchor="w",
                font=("Arial", 9),
            )

    def _render_board(self, tick: int) -> None:
        metric = self.metric_var.get()
        rows: list[tuple[float, str]] = []
        for tl in self.timelines:
            st = self._state_at_tick(tl, tick)
            if st is None:
                st = HeroState(None, None, 0, 0, 0.0, 0.0, 0, 0, 0, 0, 0)
            kills = self._kills_at_tick(tl, tick)
            deaths = st.total_deaths
            assists = tl.final_assists

            if metric == "资产总额":
                sort_val = float(st.net_worth)
                value = f"{st.net_worth}"
            elif metric == "K/D/A":
                sort_val = float(kills * 100000 - deaths * 100 + assists)
                value = f"{kills}/{deaths}/{assists}"
            elif metric == "正补/反补":
                sort_val = float(st.lh * 1000 + st.dn)
                value = f"{st.lh}/{st.dn}"
            else:
                sort_val = float(st.level)
                value = f"{st.level}"

            name = tl.player_name or short_hero_name(tl.hero_name)
            rows.append((sort_val, f"{name:14.14s}  {value:>8}"))

        rows.sort(key=lambda x: x[0], reverse=True)
        self.board_list.delete(0, tk.END)
        for _score, text in rows:
            self.board_list.insert(tk.END, text)

    def _render_status(self, tick: int) -> None:
        self.status_canvas.delete("all")
        row_h = 64
        self.status_canvas.configure(scrollregion=(0, 0, 320, row_h * len(self.timelines) + 20))

        for idx, tl in enumerate(sorted(self.timelines, key=lambda x: (x.team, x.player_id))):
            y = 16 + idx * row_h
            st = self._state_at_tick(tl, tick)
            if st is None:
                st = HeroState(None, None, 0, 0, 0.0, 0.0, 0, 0, 0, 0, 0)
            is_dead, remain_ticks = self._death_info_at_tick(tl, tick)
            remain_sec = "?" if remain_ticks is None else f"{remain_ticks / self.tick_rate:.1f}"

            self.status_canvas.create_rectangle(0, y - 10, 320, y + 48, fill="#10151b", outline="#232a33")
            self.status_canvas.create_oval(10, y, 46, y + 36, fill="#2f3946", outline="#6b7786")
            self.status_canvas.create_text(28, y + 18, text=avatar_text(tl.hero_name), fill="#fff", font=("Arial", 9, "bold"))
            if is_dead:
                self.status_canvas.create_rectangle(38, y + 26, 74, y + 40, fill="#f44336", outline="#fff")
                self.status_canvas.create_text(56, y + 33, text=f"{remain_sec}s", fill="#fff", font=("Arial", 8, "bold"))

            name = tl.player_name or short_hero_name(tl.hero_name)
            self.status_canvas.create_text(
                56, y + 8,
                text=f"{name} ({short_hero_name(tl.hero_name)})",
                fill="#e8e8e8",
                anchor="w",
                font=("Arial", 10, "bold"),
            )
            self.status_canvas.create_text(
                56, y + 24,
                text=f"HP {max(st.hp, 0)}/{max(st.max_hp, 0)}   MP {max(int(st.mana), 0)}/{max(int(st.max_mana), 0)}",
                fill="#9cd7ff",
                anchor="w",
                font=("Arial", 9),
            )
            if is_dead:
                self.status_canvas.create_text(
                    56, y + 38,
                    text=f"死亡中 · 复活剩余 {remain_sec}s",
                    fill="#ff8e8e",
                    anchor="w",
                    font=("Arial", 9),
                )

    def _render_tick(self, tick: int) -> None:
        tick = max(0, min(self.game_end_tick, int(tick)))
        self.current_tick = tick
        game_seconds = tick_to_game_time_seconds(tick, self.game_start_tick, self.tick_rate)
        self.tick_label.config(
            text=f"Tick: {tick} | 游戏时间: {format_game_time(game_seconds)} | 游戏开始 tick: {self.game_start_tick}"
        )

        self._render_map(tick)
        self._render_board(tick)
        self._render_status(tick)
        self._set_scale_value(tick)

    def on_seek(self, value: str) -> None:
        if self._suppress_scale_callback:
            return
        self._render_tick(int(float(value)))

    def _get_fps(self) -> int:
        try:
            fps = int(float(self.fps_var.get()))
        except Exception:
            fps = self.default_fps
        fps = max(1, min(240, fps))
        self.fps_var.set(str(fps))
        return fps

    def _on_fps_change(self) -> None:
        if self.playing:
            self._stop_playback()
            self._start_playback()

    def toggle_play(self) -> None:
        if self.playing:
            self._stop_playback()
        else:
            self._start_playback()

    def _start_playback(self) -> None:
        self.playing = True
        self.play_btn.config(text="暂停")
        self._schedule_next_tick()

    def _stop_playback(self) -> None:
        self.playing = False
        self.play_btn.config(text="播放")
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
            self.after_id = None

    def _schedule_next_tick(self) -> None:
        if not self.playing:
            return
        next_tick = self.current_tick + 1
        if next_tick > self.game_end_tick:
            self._stop_playback()
            return
        self._render_tick(next_tick)
        delay_ms = max(int(round(1000.0 / self._get_fps())), 1)
        self.after_id = self.root.after(delay_ms, self._schedule_next_tick)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    args = parse_args()
    replay_path = resolve_input_path(args.input_replay)
    app = ReplayPositionTkGUI(
        replay_path=replay_path,
        width=args.width,
        height=args.height,
        default_fps=args.fps,
    )
    app.run()


if __name__ == "__main__":
    main()
