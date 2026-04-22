#!/usr/bin/env python3
"""
提取 Dota 2 Source2 回放(.dem / .dem.bz2)并按 tick 导出为 JSON。

功能:
1) 提取回放可解析的所有信息表
2) 按每个 tick 输出为一个 JSON 文件中的 ticks 列表
3) 支持按变量名筛选输出字段
4) 支持按游戏时间范围筛选
5) 生成变量说明表(Markdown)
"""

from __future__ import annotations

import argparse
import bz2
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import gem


FIELD_DESCRIPTIONS = {
    "tick": "服务器 tick 编号（离散时间步）。",
    "start_tick": "事件开始 tick。",
    "end_tick": "事件结束 tick。",
    "last_death_tick": "团战内最后一次死亡发生的 tick。",
    "first_death_tick": "团战内第一次死亡发生的 tick。",
    "expires_tick": "对象（如守卫）过期/消失 tick。",
    "killed_tick": "对象被击杀的 tick。",
    "minute": "按分钟聚合的时间索引（分钟）。",
    "match_id": "比赛唯一 ID。",
    "game_mode": "游戏模式 ID。",
    "leagueid": "联赛 ID。",
    "radiant_win": "天辉是否获胜。",
    "game_start_tick": "正式开局对应的 tick（游戏时间 0 秒）。",
    "game_end_tick": "比赛结束 tick。",
    "player_id": "玩家槽位/玩家编号。",
    "player_slot": "玩家位（聊天/回放槽位）。",
    "player_name": "玩家昵称。",
    "hero_id": "英雄 ID。",
    "hero_name": "英雄内部名。",
    "team": "所属阵营（Radiant/Dire）。",
    "activator": "事件触发者（例如开雾者）。",
    "smoked": "开雾影响到的玩家列表或标识。",
    "placer": "插眼者。",
    "ward_type": "守卫类型（真假眼等）。",
    "channel": "聊天频道。",
    "text": "聊天文本。",
    "type": "目标/事件类型。",
    "name": "目标对象名。",
    "killer": "击杀者名称。",
    "state": "信使状态。",
    "flying": "信使是否处于飞行状态。",
    "x": "地图 X 坐标。",
    "y": "地图 Y 坐标。",
    "gold": "当前金钱。",
    "total_earned_gold": "累计获得金钱。",
    "total_earned_xp": "累计获得经验。",
    "net_worth": "当前经济（净资产）。",
    "xp": "当前经验值。",
    "kills": "击杀数。",
    "deaths": "死亡数。",
    "assists": "助攻数。",
    "lh": "正补数（last hits）。",
    "dn": "反补数（denies）。",
    "stuns_dealt": "造成眩晕总时长。",
    "lane_role": "分路角色。",
    "lane_last_hits": "对线期正补。",
    "lane_denies": "对线期反补。",
    "lane_total_gold": "对线期总金钱。",
    "lane_total_xp": "对线期总经验。",
    "lane_efficiency_pct": "对线效率百分比。",
    "lane_gold_adv": "对线期金钱优势。",
    "lane_xp_adv": "对线期经验优势。",
    "damage_physical": "物理伤害。",
    "damage_magical": "魔法伤害。",
    "damage_pure": "纯粹伤害。",
    "damage": "总伤害。",
    "damage_taken_physical": "承受物理伤害。",
    "damage_taken_magical": "承受魔法伤害。",
    "damage_taken_pure": "承受纯粹伤害。",
    "healing": "治疗量。",
    "self_healing": "自我治疗量。",
    "tower_damage": "对建筑伤害。",
    "total_steps": "移动步长统计。",
    "deaths": "死亡数。",
    "log_type": "战斗日志类型。",
    "attacker_name": "攻击者名称。",
    "target_name": "目标名称。",
    "inflictor_name": "技能/物品来源名。",
    "value": "日志数值。",
    "value_name": "日志值语义名称。",
    "damage_type": "伤害类型。",
    "stun_duration": "眩晕时长。",
    "ability_level": "技能等级。",
    "gold_reason": "金钱变化原因。",
    "xp_reason": "经验变化原因。",
    "attacker_is_hero": "攻击者是否英雄。",
    "target_is_hero": "目标是否英雄。",
    "attacker_is_illusion": "攻击者是否幻象。",
    "target_is_illusion": "目标是否幻象。",
    "radiant_gold_adv": "天辉金钱优势。",
    "radiant_xp_adv": "天辉经验优势。",
    "deaths": "团战死亡总数。",
    "radiant_kills": "团战天辉击杀。",
    "dire_kills": "团战夜魇击杀。",
    "winner": "团战胜方。",
    "centroid_x": "团战中心 X。",
    "centroid_y": "团战中心 Y。",
    "players": "参与该团战的玩家结构数据。",
    "slot_index": "BP 阶段槽位序号。",
    "is_pick": "是否为选人（否则为禁用）。",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="提取 Dota2 回放并按 tick 导出 JSON（支持字段与时间过滤）"
    )
    parser.add_argument("input_replay", help="输入回放路径(.dem 或 .dem.bz2)")
    parser.add_argument(
        "--output-json",
        default="replay_tick_data.json",
        help="输出 JSON 文件路径（默认: replay_tick_data.json）",
    )
    parser.add_argument(
        "--output-schema",
        default="replay_variable_table.md",
        help="输出变量说明表 Markdown 路径（默认: replay_variable_table.md）",
    )
    parser.add_argument(
        "--variables",
        default="",
        help="变量筛选，逗号分隔。支持: col 或 table.col（如 gold,players.net_worth）",
    )
    parser.add_argument(
        "--start-game-time",
        type=float,
        default=None,
        help="筛选起始游戏时间（秒，可为负数，默认不限制）",
    )
    parser.add_argument(
        "--end-game-time",
        type=float,
        default=None,
        help="筛选结束游戏时间（秒，默认不限制）",
    )
    parser.add_argument(
        "--dense-ticks",
        action="store_true",
        help="按连续 tick 输出（无数据 tick 也输出空 tables）",
    )
    return parser.parse_args()


def coerce_json_value(value: Any) -> Any:
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
    return value


def parse_variable_filter(raw: str) -> set[str]:
    if not raw.strip():
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


def keep_column(table: str, column: str, selected: set[str]) -> bool:
    if not selected:
        return True
    return column in selected or f"{table}.{column}" in selected


def tick_to_game_time_seconds(tick: int, game_start_tick: int, tick_rate: float) -> float:
    return (tick - game_start_tick) / tick_rate


def game_time_seconds_to_tick(
    game_time_seconds: float, game_start_tick: int, tick_rate: float
) -> int:
    return int(round(game_start_tick + game_time_seconds * tick_rate))


def format_game_time(seconds: float) -> str:
    sign = "-" if seconds < 0 else ""
    seconds_abs = abs(seconds)
    mm = int(seconds_abs // 60)
    ss = seconds_abs - mm * 60
    return f"{sign}{mm:02d}:{ss:06.3f}"


def ensure_dem_path(input_path: Path) -> Path:
    if input_path.suffix != ".bz2":
        return input_path

    output_dem = input_path.with_suffix("")
    print(f"[info] 解压 .bz2 -> {output_dem}")
    with bz2.open(input_path, "rb") as src, output_dem.open("wb") as dst:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            dst.write(chunk)
    return output_dem


def compute_tick_rate(match: Any) -> float:
    duration_ticks = max(match.game_end_tick - match.game_start_tick, 1)
    duration_seconds = getattr(match, "duration_seconds", None)
    if duration_seconds and duration_seconds > 0:
        return duration_ticks / duration_seconds
    return 30.0


def derive_row_tick(row: dict[str, Any], game_start_tick: int, tick_rate: float) -> int | None:
    if "tick" in row and row["tick"] is not None:
        return int(row["tick"])
    if "start_tick" in row and row["start_tick"] is not None:
        return int(row["start_tick"])
    if "minute" in row and row["minute"] is not None:
        return game_time_seconds_to_tick(float(row["minute"]) * 60.0, game_start_tick, tick_rate)
    return None


def describe_field(table: str, column: str) -> str:
    key = f"{table}.{column}"
    if key in FIELD_DESCRIPTIONS:
        return FIELD_DESCRIPTIONS[key]
    if column in FIELD_DESCRIPTIONS:
        return FIELD_DESCRIPTIONS[column]
    return f"{table} 表中的 `{column}` 字段。"


def write_schema_markdown(
    schema_path: Path, dfs: dict[str, Any], selected_variables: set[str]
) -> None:
    rows: list[tuple[str, str, str]] = []
    for table in sorted(dfs.keys()):
        df = dfs[table]
        if len(getattr(df, "columns", [])) == 0:
            continue
        for column in df.columns:
            if selected_variables and not keep_column(table, column, selected_variables):
                continue
            rows.append((table, str(column), describe_field(table, str(column))))

    rows.extend(
        [
            ("__output__", "tick", "当前输出块对应的 tick。"),
            (
                "__output__",
                "game_time_seconds",
                "将 tick 换算后的游戏时间（秒）。",
            ),
            ("__output__", "game_time", "将秒格式化后的游戏时间（MM:SS.mmm）。"),
        ]
    )

    lines = [
        "# 变量说明表",
        "",
        "| 来源表 | 变量名 | 说明 |",
        "|---|---|---|",
    ]
    for table, column, desc in rows:
        lines.append(f"| {table} | {column} | {desc} |")
    schema_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    input_path = Path(args.input_replay).expanduser().resolve()
    output_json = Path(args.output_json).expanduser().resolve()
    output_schema = Path(args.output_schema).expanduser().resolve()

    if not input_path.exists():
        raise FileNotFoundError(f"输入文件不存在: {input_path}")

    selected_variables = parse_variable_filter(args.variables)

    dem_path = ensure_dem_path(input_path)
    print(f"[info] 解析回放: {dem_path}")

    match = gem.parse(str(dem_path))
    dfs = gem.parse_to_dataframe(str(dem_path))

    tick_rate = compute_tick_rate(match)
    game_start_tick = int(match.game_start_tick)

    # tick <-> 游戏时间 换算关系（以 game_start_tick 作为 0 秒）:
    # game_time_seconds = (tick - game_start_tick) / tick_rate
    # tick = round(game_start_tick + game_time_seconds * tick_rate)
    filter_start_tick = (
        game_time_seconds_to_tick(args.start_game_time, game_start_tick, tick_rate)
        if args.start_game_time is not None
        else None
    )
    filter_end_tick = (
        game_time_seconds_to_tick(args.end_game_time, game_start_tick, tick_rate)
        if args.end_game_time is not None
        else None
    )

    tick_tables: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    non_tick_tables: dict[str, list[dict[str, Any]]] = defaultdict(list)
    min_seen_tick: int | None = None
    max_seen_tick: int | None = None

    for table_name, df in dfs.items():
        if len(getattr(df, "columns", [])) == 0:
            continue
        for row in df.to_dict(orient="records"):
            row_tick = derive_row_tick(row, game_start_tick, tick_rate)

            filtered_row = {
                col: coerce_json_value(value)
                for col, value in row.items()
                if keep_column(table_name, col, selected_variables)
            }
            if selected_variables and not filtered_row:
                continue

            if row_tick is None:
                non_tick_tables[table_name].append(filtered_row)
                continue

            if filter_start_tick is not None and row_tick < filter_start_tick:
                continue
            if filter_end_tick is not None and row_tick > filter_end_tick:
                continue

            tick_tables[row_tick][table_name].append(filtered_row)
            min_seen_tick = row_tick if min_seen_tick is None else min(min_seen_tick, row_tick)
            max_seen_tick = row_tick if max_seen_tick is None else max(max_seen_tick, row_tick)

    if args.dense_ticks:
        if filter_start_tick is not None and filter_end_tick is not None:
            dense_start, dense_end = filter_start_tick, filter_end_tick
        else:
            dense_start = min_seen_tick if min_seen_tick is not None else int(match.game_start_tick)
            dense_end = max_seen_tick if max_seen_tick is not None else int(match.game_end_tick)
        tick_sequence = range(dense_start, dense_end + 1)
    else:
        tick_sequence = sorted(tick_tables.keys())

    ticks_output: list[dict[str, Any]] = []
    for tick in tick_sequence:
        game_time_seconds = tick_to_game_time_seconds(int(tick), game_start_tick, tick_rate)
        tables = dict(tick_tables.get(int(tick), {}))
        ticks_output.append(
            {
                "tick": int(tick),
                "game_time_seconds": round(game_time_seconds, 3),
                "game_time": format_game_time(game_time_seconds),
                "tables": tables,
            }
        )

    output_payload = {
        "meta": {
            "input_replay": str(input_path),
            "dem_path_used": str(dem_path),
            "match_id": int(match.match_id),
            "game_start_tick": int(match.game_start_tick),
            "game_end_tick": int(match.game_end_tick),
            "duration_seconds": float(match.duration_seconds),
            "tick_rate": tick_rate,
            "tick_game_time_relation": {
                "formula_game_time_seconds": "(tick - game_start_tick) / tick_rate",
                "formula_tick": "round(game_start_tick + game_time_seconds * tick_rate)",
            },
            "filter": {
                "variables": sorted(selected_variables),
                "start_game_time_seconds": args.start_game_time,
                "end_game_time_seconds": args.end_game_time,
                "dense_ticks": bool(args.dense_ticks),
            },
        },
        "non_tick_tables": non_tick_tables,
        "ticks": ticks_output,
    }

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(output_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_schema_markdown(output_schema, dfs, selected_variables)

    print(f"[done] JSON 输出: {output_json}")
    print(f"[done] 变量说明表: {output_schema}")
    print(f"[done] tick 数量: {len(ticks_output)}")


if __name__ == "__main__":
    main()
