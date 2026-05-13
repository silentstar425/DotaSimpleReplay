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
from tkinter import messagebox
import time

import gem
from gem.extractors.players import PlayerExtractor
from gem.parser import ReplayParser
from replay_cache import cache_path_for_dem, delete_replay_cache, load_replay_cache, save_replay_cache
from replay_download_io import iter_default_replay_candidates, migrate_legacy_replay_samples_to_replays
from replay_world_entities import WorldEntityCollector


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


@dataclass
class WorldEntityState:
    x: float | None
    y: float | None
    hp: int
    max_hp: int
    active: bool


@dataclass
class WorldEntityTimeline:
    entity_id: int
    entity_name: str
    class_name: str
    team: int
    category: str
    subtype: str
    ticks: list[int] = field(default_factory=list)
    states: list[WorldEntityState] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dota2 回放增强 GUI（Tkinter 版）")
    parser.add_argument(
        "input_replay",
        nargs="?",
        default=None,
        help="回放路径（.dem；本机库仅使用 replays/）。不传则尝试使用 replays/ 下第一个 .dem。",
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

    migrate_legacy_replay_samples_to_replays()
    candidates = iter_default_replay_candidates()
    if not candidates:
        raise FileNotFoundError(
            "未提供 input_replay 且在 replays/ 下找不到 .dem 回放文件。"
            "请用: python3 replay_position_gui_tk.py <your.dem>"
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


def level_from_xp(total_xp: int) -> int:
    """根据累计 XP 估算英雄等级（上限 30）。"""
    xp = max(int(total_xp), 0)
    # Dota 2 常用升级阈值（累计 XP 到达该值时升到对应等级）。
    # 索引 i 表示达到 levels[i] 后为等级 i+1。
    levels = [
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
    level = 1
    for i, need in enumerate(levels, start=1):
        if xp >= need:
            level = i
        else:
            break
    return min(level, 30)


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
        self.entity_timelines: list[WorldEntityTimeline] = []

        self.playing = False
        self.after_id: str | None = None
        self._suppress_scale_callback = False
        self.current_tick_float = 0.0
        self.playback_anchor_real_s = 0.0
        self.playback_anchor_tick = 0.0
        self.dem_path: Path | None = None
        self.cache_path: Path | None = None
        self.cache_hit = False
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_label_var = tk.StringVar(value="解析进度：等待中")

        self.root = tk.Tk()
        self.root.title("Dota2 回放增强 GUI（Tkinter 版）")
        self.root.geometry(f"{self.window_width}x{self.window_height}")

        self._build_ui()
        self._load_replay()
        self._render_tick_float(0.0)

    def _build_ui(self) -> None:
        container = tk.Frame(self.root, bg="#0f1116")
        container.pack(fill=tk.BOTH, expand=True)

        self.left_panel = tk.Frame(container, bg="#161b22", width=261)
        self.left_panel.pack(side=tk.LEFT, fill=tk.Y)
        self.left_panel.pack_propagate(False)

        self.center_panel = tk.Frame(container, bg="#0f1116")
        self.center_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.right_panel = tk.Frame(container, bg="#161b22", width=306)
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
            font=("Arial", 12, "bold"),
            anchor="w",
        ).pack(fill=tk.X, padx=10, pady=(10, 6))

        tk.Label(
            self.left_panel,
            text="排序指标",
            fg="#a8b2bf",
            bg="#161b22",
            font=("Arial", 9),
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
            font=("Consolas", 10),
        )
        self.board_list.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

    def _build_center_panel(self) -> None:
        top = tk.Frame(self.center_panel, bg="#0f1116")
        top.pack(fill=tk.X, padx=9, pady=7)

        self.tick_label = tk.Label(top, text="Tick: -", fg="#f2f2f2", bg="#0f1116", anchor="w")
        self.tick_label.pack(fill=tk.X)

        controls = tk.Frame(self.center_panel, bg="#0f1116")
        controls.pack(fill=tk.X, padx=9, pady=(0, 7))

        self.play_btn = tk.Button(
            controls,
            text="播放",
            width=9,
            bg="#2d6cdf",
            fg="#ffffff",
            relief=tk.FLAT,
            command=self.toggle_play,
        )
        self.play_btn.pack(side=tk.LEFT, padx=(0, 9))

        self.clear_cache_btn = tk.Button(
            controls,
            text="清理缓存",
            width=9,
            bg="#8b1e2d",
            fg="#ffffff",
            relief=tk.FLAT,
            command=self._clear_cache_with_confirm,
        )
        self.clear_cache_btn.pack(side=tk.LEFT, padx=(0, 9))

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

        tk.Label(controls, text="刷新率(FPS)", fg="#9fb0c1", bg="#0f1116").pack(side=tk.LEFT, padx=(9, 5))
        self.fps_var = tk.StringVar(value=str(self.default_fps))
        self.fps_spin = tk.Spinbox(
            controls,
            from_=1,
            to=240,
            increment=1,
            width=5,
            textvariable=self.fps_var,
            command=self._on_fps_change,
            bg="#11161d",
            fg="#e8e8e8",
            relief=tk.FLAT,
        )
        self.fps_spin.pack(side=tk.LEFT)

        progress_row = tk.Frame(self.center_panel, bg="#0f1116")
        progress_row.pack(fill=tk.X, padx=9, pady=(0, 6))
        self.progress_label = tk.Label(
            progress_row,
            textvariable=self.progress_label_var,
            fg="#9ea7b3",
            bg="#0f1116",
            anchor="w",
            font=("Arial", 9),
        )
        self.progress_label.pack(side=tk.LEFT, padx=(0, 8))
        self.progress_bar = ttk.Progressbar(
            progress_row,
            orient=tk.HORIZONTAL,
            mode="determinate",
            length=220,
            variable=self.progress_var,
            maximum=100.0,
        )
        self.progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.canvas = tk.Canvas(
            self.center_panel,
            bg="#111111",
            highlightthickness=1,
            highlightbackground="#414a56",
        )
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=9, pady=(0, 5))

        tk.Label(
            self.center_panel,
            text=(
                "英雄：绿/红圆点；建筑：基/塔/营/建；小兵与野怪：近/远/车/野；"
                "莲花池/肉山/折磨者：莲/肉/折；死亡英雄不会显示"
            ),
            fg="#9ea7b3",
            bg="#0f1116",
            anchor="w",
        ).pack(fill=tk.X, padx=9, pady=(0, 9))

    def _build_right_panel(self) -> None:
        tk.Label(
            self.right_panel,
            text="英雄状态",
            fg="#e8e8e8",
            bg="#161b22",
            font=("Arial", 12, "bold"),
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
        self.status_canvas.pack(fill=tk.BOTH, expand=True, padx=9, pady=(0, 9))

    def _load_replay(self) -> None:
        print(f"[info] 读取回放: {self.replay_path}")
        dem_path = ensure_dem_path(self.replay_path)
        self.dem_path = dem_path
        self.cache_path = cache_path_for_dem(dem_path)
        print(f"[info] 解析 DEM: {dem_path}")
        self.progress_var.set(0.0)
        self.progress_label_var.set("解析进度：检查缓存")
        self.root.update_idletasks()

        cached = load_replay_cache(dem_path)
        if cached is not None:
            payload = dict(cached)
            self.cache_hit = True
            print(f"[info] 命中缓存: {self.cache_path}")
            self.progress_var.set(100.0)
            self.progress_label_var.set("解析进度：缓存命中")
        else:
            self.cache_hit = False
            self.progress_label_var.set("解析进度：解析中 0.0%")
            self.root.update_idletasks()
            payload = self._build_payload_from_dem(dem_path)
            save_replay_cache(dem_path, payload)
            print(f"[info] 已写入缓存: {self.cache_path}")
            self.progress_var.set(100.0)
            self.progress_label_var.set("解析进度：完成")
        self._apply_cached_payload(payload)
        match_id = int(payload.get("match_id", 0))

        self.scale.config(from_=0, to=self.game_end_tick)
        self._set_scale_value(0)

        print(
            f"[info] 回放范围: 0 -> {self.game_end_tick} (game_start_tick={self.game_start_tick}), "
            f"tick_rate={self.tick_rate:.2f}, 玩家轨迹={len(self.timelines)}, 世界实体轨迹={len(self.entity_timelines)}"
        )

    def _build_payload_from_dem(self, dem_path: Path) -> dict[str, Any]:
        match = gem.parse(str(dem_path))
        parser = ReplayParser(str(dem_path))
        # 使用逐 tick 采样，确保刷新率提升时有足够细粒度的数据可更新。
        player_ext = PlayerExtractor(sample_interval=1, minute_snapshots=False)
        player_ext.attach(parser)
        world_ext = WorldEntityCollector(sample_interval=6)
        world_ext.attach(parser)

        estimated_end_tick = max(int(match.game_end_tick), 1)
        last_tick = -10**9

        def _progress_callback(_entity: Any, _op: Any) -> None:
            nonlocal last_tick
            tick_now = int(parser.tick)
            if tick_now - last_tick < 180:
                return
            last_tick = tick_now
            pct = max(0.0, min(100.0, 100.0 * tick_now / float(estimated_end_tick)))
            self.progress_var.set(pct)
            self.progress_label_var.set(f"解析进度：{pct:.1f}%")
            self.root.update_idletasks()

        parser.on_entity(_progress_callback)
        parser.parse()
        self.progress_var.set(100.0)
        self.progress_label_var.set("解析进度：构建地图实体轨迹")
        self.root.update_idletasks()

        by_pid: dict[int, dict[str, Any]] = {}
        hero_to_pid: dict[str, int] = {}
        for pp in match.players:
            pid = int(pp.player_id)
            by_pid[pid] = {
                "player_id": pid,
                "player_name": str(pp.player_name or ""),
                "hero_name": str(pp.hero_name),
                "team": int(pp.team),
                "final_kills": int(pp.kills),
                "final_deaths": int(pp.deaths),
                "final_assists": int(pp.assists),
                "kill_event_ticks": [],
                "ticks": [],
                "states": [],
            }
            if pp.hero_name:
                hero_to_pid[str(pp.hero_name).lower()] = pid

        for entry in match.combat_log:
            if (
                entry.log_type == "DEATH"
                and entry.attacker_is_hero
                and entry.target_is_hero
                and entry.attacker_name
            ):
                pid = hero_to_pid.get(entry.attacker_name.lower())
                if pid is not None and pid in by_pid:
                    by_pid[pid]["kill_event_ticks"].append(int(entry.tick))

        min_x = float("inf")
        max_x = float("-inf")
        min_y = float("inf")
        max_y = float("-inf")
        for snap in player_ext.snapshots:
            pid = int(snap.player_id)
            slot = by_pid.get(pid)
            if slot is None:
                continue
            state = {
                "x": None if snap.x is None else float(snap.x),
                "y": None if snap.y is None else float(snap.y),
                "hp": int(snap.hp),
                "max_hp": int(snap.max_hp),
                "mana": float(snap.mana),
                "max_mana": float(snap.max_mana),
                "level": level_from_xp(int(snap.xp)),
                "net_worth": int(snap.net_worth),
                "lh": int(snap.lh),
                "dn": int(snap.dn),
                "total_deaths": int(snap.total_deaths),
            }
            slot["ticks"].append(int(snap.tick))
            slot["states"].append(state)
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

        if min_x == float("inf"):
            min_x, max_x, min_y, max_y = 0.0, 1.0, 0.0, 1.0

        return {
            "match_id": int(match.match_id),
            "game_start_tick": int(match.game_start_tick),
            "game_end_tick": max(int(match.game_end_tick), int(parser.tick)),
            "tick_rate": compute_tick_rate(match),
            "map_bounds": {
                "min_x": float(min_x),
                "max_x": float(max_x),
                "min_y": float(min_y),
                "max_y": float(max_y),
            },
            "player_timelines": [by_pid[k] for k in sorted(by_pid.keys())],
            "entity_timelines": entity_timelines,
        }

    def _apply_cached_payload(self, payload: dict[str, Any]) -> None:
        self.tick_rate = float(payload["tick_rate"])
        self.game_start_tick = int(payload["game_start_tick"])
        self.game_end_tick = int(payload["game_end_tick"])
        self.current_tick = 0
        self.current_tick_float = 0.0

        bounds = payload.get("map_bounds", {})
        self.min_x = float(bounds.get("min_x", 0.0))
        self.max_x = float(bounds.get("max_x", 1.0))
        self.min_y = float(bounds.get("min_y", 0.0))
        self.max_y = float(bounds.get("max_y", 1.0))

        timelines: list[PlayerTimeline] = []
        for row in payload.get("player_timelines", []):
            tl = PlayerTimeline(
                player_id=int(row["player_id"]),
                player_name=str(row.get("player_name", "")),
                hero_name=str(row.get("hero_name", "")),
                team=int(row.get("team", 0)),
                final_kills=int(row.get("final_kills", 0)),
                final_deaths=int(row.get("final_deaths", 0)),
                final_assists=int(row.get("final_assists", 0)),
                kill_event_ticks=[int(x) for x in row.get("kill_event_ticks", [])],
                ticks=[int(x) for x in row.get("ticks", [])],
                states=[
                    HeroState(
                        x=(None if s.get("x") is None else float(s["x"])),
                        y=(None if s.get("y") is None else float(s["y"])),
                        hp=int(s.get("hp", 0)),
                        max_hp=int(s.get("max_hp", 0)),
                        mana=float(s.get("mana", 0.0)),
                        max_mana=float(s.get("max_mana", 0.0)),
                        level=int(s.get("level", 0)),
                        net_worth=int(s.get("net_worth", 0)),
                        lh=int(s.get("lh", 0)),
                        dn=int(s.get("dn", 0)),
                        total_deaths=int(s.get("total_deaths", 0)),
                    )
                    for s in row.get("states", [])
                ],
            )
            tl.death_windows = build_death_windows(tl.ticks, tl.states)
            timelines.append(tl)
        self.timelines = timelines
        entities: list[WorldEntityTimeline] = []
        for row in payload.get("entity_timelines", []):
            entities.append(
                WorldEntityTimeline(
                    entity_id=int(row.get("entity_id", 0)),
                    entity_name=str(row.get("entity_name", "")),
                    class_name=str(row.get("class_name", "")),
                    team=int(row.get("team", 0)),
                    category=str(row.get("category", "other")),
                    subtype=str(row.get("subtype", "other")),
                    ticks=[int(x) for x in row.get("ticks", [])],
                    states=[
                        WorldEntityState(
                            x=(None if s.get("x") is None else float(s["x"])),
                            y=(None if s.get("y") is None else float(s["y"])),
                            hp=int(s.get("hp", 0)),
                            max_hp=int(s.get("max_hp", 0)),
                            active=bool(s.get("active", False)),
                        )
                        for s in row.get("states", [])
                    ],
                )
            )
        self.entity_timelines = entities

    def _clear_cache_with_confirm(self) -> None:
        if self.dem_path is None or self.cache_path is None:
            messagebox.showinfo("清理缓存", "当前录像未就绪，无法清理缓存。")
            return
        ok1 = messagebox.askyesno(
            "清理缓存",
            "确定要删除当前录像的缓存文件吗？该操作不可撤销。",
        )
        if not ok1:
            return
        ok2 = messagebox.askyesno(
            "二次确认",
            "请再次确认：删除后下次将重新解析录像，可能较慢。是否继续？",
        )
        if not ok2:
            return
        deleted = delete_replay_cache(self.dem_path)
        if deleted:
            self.cache_hit = False
            messagebox.showinfo("清理缓存", f"缓存已删除：{self.cache_path}")
        else:
            messagebox.showinfo("清理缓存", f"未删除缓存（可能不存在）：{self.cache_path}")

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

    def _world_state_at_tick(self, tl: WorldEntityTimeline, tick: int) -> WorldEntityState | None:
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

    @staticmethod
    def _entity_glyph(tl: WorldEntityTimeline) -> str:
        if tl.category == "building":
            if tl.subtype == "base":
                return "基"
            if tl.subtype == "tower":
                return "塔"
            if tl.subtype == "barracks":
                return "营"
            return "建"
        if tl.category == "creep":
            if tl.subtype == "melee":
                return "近"
            if tl.subtype == "ranged":
                return "远"
            if tl.subtype == "siege":
                return "车"
            if tl.subtype == "neutral":
                return "野"
            return "兵"
        if tl.category == "lotus_pool":
            return "莲"
        if tl.category == "roshan":
            return "肉"
        if tl.category == "tormentor":
            return "折"
        if tl.category == "ward":
            return ""
        return "?"

    @staticmethod
    def _entity_colors(tl: WorldEntityTimeline) -> tuple[str, str]:
        if tl.category == "building":
            if tl.subtype == "base":
                return "#f9a825", "#fff3c4"
            if tl.subtype == "tower":
                return "#ef6c00", "#ffe0b2"
            if tl.subtype == "barracks":
                return "#8d6e63", "#d7ccc8"
            return "#546e7a", "#cfd8dc"
        if tl.category == "creep":
            if tl.subtype == "melee":
                return "#78909c", "#eceff1"
            if tl.subtype == "ranged":
                return "#26a69a", "#e0f2f1"
            if tl.subtype == "siege":
                return "#607d8b", "#cfd8dc"
            if tl.subtype == "neutral":
                return "#8e24aa", "#f3e5f5"
            return "#5c6bc0", "#e8eaf6"
        if tl.category == "lotus_pool":
            return "#00acc1", "#e0f7fa"
        if tl.category == "roshan":
            return "#6d4c41", "#efebe9"
        if tl.category == "tormentor":
            return "#6a1b9a", "#f3e5f5"
        if tl.category == "ward":
            if tl.team == 2:
                return "#66bb6a", "#1b5e20"
            if tl.team == 3:
                return "#ef5350", "#b71c1c"
            return "#78909c", "#37474f"
        return "#455a64", "#eceff1"

    def _draw_world_entity(self, tl: WorldEntityTimeline, st: WorldEntityState) -> None:
        if st.x is None or st.y is None or not st.active:
            return
        cx, cy = self._map_to_canvas(st.x, st.y)
        fill, outline = self._entity_colors(tl)
        glyph = self._entity_glyph(tl)
        r = 9 if tl.category in ("roshan", "tormentor") else 5 if tl.category == "ward" else 7

        if tl.category == "ward":
            rr = int(round(r * 1.15))
            if tl.subtype == "sentry":
                points = [cx, cy - rr, cx + rr, cy, cx, cy + rr, cx - rr, cy]
                self.canvas.create_polygon(points, fill=fill, outline=outline, width=1.2)
            else:
                self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline=outline, width=1.2)
        elif tl.category == "building" and tl.subtype == "tower":
            points = [cx, cy - r, cx - r, cy + r, cx + r, cy + r]
            self.canvas.create_polygon(points, fill=fill, outline=outline, width=1.2)
        elif tl.category == "building" and tl.subtype == "barracks":
            points = [cx, cy - r, cx - r, cy, cx, cy + r, cx + r, cy]
            self.canvas.create_polygon(points, fill=fill, outline=outline, width=1.2)
        elif tl.category == "creep" and tl.subtype == "siege":
            self.canvas.create_rectangle(cx - r, cy - r * 0.7, cx + r, cy + r * 0.7, fill=fill, outline=outline, width=1.2)
        else:
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline=outline, width=1.2)

        if glyph:
            self.canvas.create_text(cx, cy, text=glyph, fill="#ffffff", font=("Arial", 8, "bold"))
        if tl.category in ("lotus_pool", "roshan", "tormentor"):
            self.canvas.create_text(
                cx + 10,
                cy - 2,
                text=tl.entity_name.replace("npc_dota_", ""),
                fill="#d9e4f0",
                anchor="w",
                font=("Arial", 8),
            )

    def _render_map(self, tick: int) -> None:
        self.canvas.delete("all")
        width = max(self.canvas.winfo_width(), 200)
        height = max(self.canvas.winfo_height(), 200)
        self.canvas.create_rectangle(20, 20, width - 20, height - 20, outline="#666", width=2)
        self.canvas.create_text(
            28,
            12,
            text="地图（归一化坐标） | 建筑: 基/塔/营/建 | 小兵: 近/远/车 | 野怪: 野 | 莲/肉/折 | 守卫: 天辉绿/夜魇红 假眼圆/真眼菱形",
            fill="#ccc",
            anchor="w",
        )

        for etl in self.entity_timelines:
            est = self._world_state_at_tick(etl, tick)
            if est is None:
                continue
            self._draw_world_entity(etl, est)

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
        self.canvas.create_text(
            30,
            height - 12,
            text="建筑: 基/塔/营/建 | 单位: 近/远/车/野 | 莲花池:莲 | 肉山:肉 | 折磨者:折",
            fill="#9ea7b3",
            anchor="w",
            font=("Arial", 8),
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
        row_h = 58
        self.status_canvas.configure(scrollregion=(0, 0, 290, row_h * len(self.timelines) + 18))

        for idx, tl in enumerate(sorted(self.timelines, key=lambda x: (x.team, x.player_id))):
            y = 14 + idx * row_h
            st = self._state_at_tick(tl, tick)
            if st is None:
                st = HeroState(None, None, 0, 0, 0.0, 0.0, 0, 0, 0, 0, 0)
            is_dead, remain_ticks = self._death_info_at_tick(tl, tick)
            remain_sec = "?" if remain_ticks is None else f"{remain_ticks / self.tick_rate:.1f}"

            self.status_canvas.create_rectangle(0, y - 9, 290, y + 44, fill="#10151b", outline="#232a33")
            self.status_canvas.create_oval(9, y, 41, y + 32, fill="#2f3946", outline="#6b7786")
            if is_dead:
                self.status_canvas.create_oval(9, y, 41, y + 32, fill="#b52323", outline="#ffffff")
                self.status_canvas.create_text(25, y + 16, text=f"{remain_sec}s", fill="#fff", font=("Arial", 8, "bold"))
            else:
                self.status_canvas.create_text(25, y + 16, text=avatar_text(tl.hero_name), fill="#fff", font=("Arial", 8, "bold"))

            hero = short_hero_name(tl.hero_name)
            name = tl.player_name or hero
            if is_dead:
                title_color = "#ff9a9a"
            else:
                title_color = "#e8e8e8"
            self.status_canvas.create_text(49, y + 8, text=f"{hero} ({name})", fill=title_color, anchor="w", font=("Arial", 9, "bold"))
            self.status_canvas.create_text(
                49, y + 22,
                text=f"HP {max(st.hp, 0)}/{max(st.max_hp, 0)}   MP {max(int(st.mana), 0)}/{max(int(st.max_mana), 0)}",
                fill="#9cd7ff",
                anchor="w",
                font=("Arial", 8),
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

    def _render_tick_float(self, tick_float: float) -> None:
        clamped = max(0.0, min(float(self.game_end_tick), float(tick_float)))
        self.current_tick_float = clamped
        self._render_tick(int(round(clamped)))

    def on_seek(self, value: str) -> None:
        if self._suppress_scale_callback:
            return
        self._render_tick_float(float(value))
        if self.playing:
            self.playback_anchor_real_s = time.perf_counter()
            self.playback_anchor_tick = self.current_tick_float

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
        self.playback_anchor_real_s = time.perf_counter()
        self.playback_anchor_tick = self.current_tick_float
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
        elapsed_s = time.perf_counter() - self.playback_anchor_real_s
        target_tick_float = self.playback_anchor_tick + elapsed_s * self.tick_rate
        if target_tick_float >= self.game_end_tick:
            self._render_tick_float(float(self.game_end_tick))
            self._stop_playback()
            return
        self._render_tick_float(target_tick_float)
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
