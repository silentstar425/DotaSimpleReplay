#!/usr/bin/env python3
"""
回放中的非英雄地图实体提取与分类工具。

提取目标：
- 建筑（基地/防御塔/兵营/其他）
- 兵线与野怪（近战兵/远程兵/工程车/野怪）
- 莲花池
- 肉山
- 折磨者
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gem.entities import Entity, EntityOp
from gem.parser import ReplayParser

_CELL_SIZE = 128.0


def _entity_pos(entity: Entity) -> tuple[float, float] | None:
    cell_x = entity.get_uint32("CBodyComponent.m_cellX")
    cell_y = entity.get_uint32("CBodyComponent.m_cellY")
    vec_x = entity.get_float32("CBodyComponent.m_vecX")
    vec_y = entity.get_float32("CBodyComponent.m_vecY")
    if cell_x is None or cell_y is None or vec_x is None or vec_y is None:
        return None
    return (cell_x * _CELL_SIZE + vec_x, cell_y * _CELL_SIZE + vec_y)


def _entity_npc_name(entity: Entity, parser: ReplayParser) -> str:
    unit_name = entity.get_string("m_iszUnitName")
    if unit_name:
        return unit_name
    idx = entity.get_int32("m_pEntity.m_nameStringableIndex")
    if idx is not None:
        names_table = parser.string_tables.get_by_name("EntityNames")
        if names_table is not None:
            item = names_table.items.get(idx)
            if item is not None and item[0]:
                return str(item[0])
    return ""


def _classify_building_subtype(name: str, class_name: str) -> str:
    if "tower" in name or "tower" in class_name:
        return "tower"
    if "rax" in name or "barracks" in name or "barracks" in class_name:
        return "barracks"
    if (
        "fort" in name
        or "fountain" in name
        or "ancient" in name
        or "fort" in class_name
        or "fountain" in class_name
        or "ancient" in class_name
    ):
        return "base"
    return "other"


def classify_world_entity(npc_name: str, class_name: str) -> tuple[str, str] | None:
    name = (npc_name or "").lower()
    cls = (class_name or "").lower()
    if not name and not cls:
        return None

    # 仅处理地图中的实体单位，排除技能、修饰器等逻辑实体。
    if cls.startswith("cdota_ability_") or "_ability_" in cls:
        return None
    if cls.startswith("cdota_modifier_") or "_modifier_" in cls:
        return None
    if (
        not cls.startswith("cdota_basenpc")
        and not cls.startswith("cdota_unit_")
        and not name.startswith("npc_dota_")
    ):
        return None

    if name.startswith("npc_dota_hero_"):
        return None

    if "lotus_pool" in name or "lotuspool" in cls:
        return ("lotus_pool", "lotus_pool")
    if "roshan" in name or "roshan" in cls:
        return ("roshan", "roshan")
    if "tormentor" in name or "tormentor" in cls or "miniboss" in name or "miniboss" in cls:
        return ("tormentor", "tormentor")

    if name.startswith("npc_dota_neutral_") or ("neutral" in cls and "creep" in cls):
        return ("creep", "neutral")

    if name.startswith("npc_dota_creep_") or ("creep" in cls and "neutral" not in cls):
        if "siege" in name or "siege" in cls or "catapult" in name:
            return ("creep", "siege")
        if "ranged" in name or "range" in name:
            return ("creep", "ranged")
        if "melee" in name:
            return ("creep", "melee")
        return ("creep", "other")

    if (
        name.startswith("npc_dota_goodguys_")
        or name.startswith("npc_dota_badguys_")
        or "building" in cls
        or "tower" in cls
        or "barracks" in cls
        or "fountain" in cls
        or "fort" in cls
    ):
        return ("building", _classify_building_subtype(name, cls))

    return None


def _classification_score(category: str, subtype: str) -> int:
    score = 0
    if category in {"building", "creep", "lotus_pool", "roshan", "tormentor"}:
        score += 10
    if subtype not in {"other", ""}:
        score += 5
    return score


def _infer_lane_creep_subtype(name: str, class_name: str, max_hp: int) -> str | None:
    full = f"{name} {class_name}".lower()
    if "siege" in full or "catapult" in full:
        return "siege"
    if "ranged" in full or "range" in full:
        return "ranged"
    if "melee" in full:
        return "melee"
    if "creep_lane" in full:
        # 在名称字段缺失时，使用生命上限粗分近战/远程。
        return "ranged" if max_hp <= 450 else "melee"
    return None


@dataclass
class _WorldEntityTimeline:
    entity_id: int
    entity_name: str
    class_name: str
    team: int
    category: str
    subtype: str
    ticks: list[int] = field(default_factory=list)
    states: list[dict[str, Any]] = field(default_factory=list)
    last_record_tick: int = -10**9


class WorldEntityCollector:
    def __init__(self, sample_interval: int = 6) -> None:
        self.sample_interval = max(1, int(sample_interval))
        self._parser: ReplayParser | None = None
        self._timelines: dict[int, _WorldEntityTimeline] = {}

    def attach(self, parser: ReplayParser) -> None:
        self._parser = parser
        parser.on_entity(self._on_entity)

    def _on_entity(self, entity: Entity, op: EntityOp) -> None:
        if self._parser is None:
            return
        tick = int(self._parser.tick)
        entity_idx = int(entity.get_index())
        timeline = self._timelines.get(entity_idx)

        npc_name = _entity_npc_name(entity, self._parser)
        class_name = entity.get_class_name()
        hp = int(entity.get_int32("m_iHealth") or 0)
        max_hp = int(entity.get_int32("m_iMaxHealth") or 0)
        classified = classify_world_entity(npc_name, class_name)

        if timeline is None:
            if classified is None:
                return
            category, subtype = classified
            inferred = _infer_lane_creep_subtype(npc_name, class_name, max_hp)
            if category == "creep" and subtype == "other" and inferred is not None:
                subtype = inferred
            timeline = _WorldEntityTimeline(
                entity_id=entity_idx,
                entity_name=npc_name or class_name,
                class_name=class_name,
                team=int(entity.get_int32("m_iTeamNum") or 0),
                category=category,
                subtype=subtype,
            )
            self._timelines[entity_idx] = timeline
        elif classified is not None:
            new_category, new_subtype = classified
            inferred = _infer_lane_creep_subtype(npc_name, class_name, max_hp)
            if new_category == "creep" and new_subtype == "other" and inferred is not None:
                new_subtype = inferred
            old_score = _classification_score(timeline.category, timeline.subtype)
            new_score = _classification_score(new_category, new_subtype)
            if new_score > old_score:
                timeline.category = new_category
                timeline.subtype = new_subtype
            if npc_name and npc_name != timeline.entity_name:
                timeline.entity_name = npc_name
            timeline.class_name = class_name or timeline.class_name
            timeline.team = int(entity.get_int32("m_iTeamNum") or timeline.team)

        force_record = op.has(EntityOp.CREATED) or op.has(EntityOp.LEFT) or op.has(EntityOp.DELETED)
        if not force_record and (tick - timeline.last_record_tick) < self.sample_interval:
            return

        pos = _entity_pos(entity)
        active = bool(entity.active and not op.has(EntityOp.LEFT) and not op.has(EntityOp.DELETED) and hp > 0)
        state = {
            "x": None if pos is None else float(pos[0]),
            "y": None if pos is None else float(pos[1]),
            "hp": hp,
            "max_hp": max_hp,
            "active": active,
        }

        if timeline.ticks and timeline.ticks[-1] == tick:
            timeline.states[-1] = state
        else:
            timeline.ticks.append(tick)
            timeline.states.append(state)
        timeline.last_record_tick = tick

    def to_payload(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in self._timelines.values():
            if not item.ticks:
                continue
            rows.append(
                {
                    "entity_id": item.entity_id,
                    "entity_name": item.entity_name,
                    "class_name": item.class_name,
                    "team": item.team,
                    "category": item.category,
                    "subtype": item.subtype,
                    "ticks": item.ticks,
                    "states": item.states,
                }
            )
        rows.sort(key=lambda x: (x["category"], x["subtype"], x["team"], x["entity_name"], x["entity_id"]))
        return rows
