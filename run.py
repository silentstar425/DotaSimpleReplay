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
from concurrent.futures import ThreadPoolExecutor
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
      width: 270px;
      background: #161b22;
      border-right: 1px solid #2b313a;
      display: flex;
      flex-direction: column;
      padding: 10px;
      box-sizing: border-box;
    }
    .side.right {
      border-right: none;
      border-left: 1px solid #2b313a;
      width: 306px;
    }
    .side h3 { margin: 0 0 10px; font-size: 14px; }
    .center {
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
      padding: 10px;
      box-sizing: border-box;
      gap: 9px;
    }
    .meta { font-size: 12px; color: #c8c8c8; }
    .meta strong { color: #ffffff; }
    .controls {
      display: flex;
      flex-direction: column;
      align-items: stretch;
      gap: 8px;
    }
    .control-row {
      display: flex;
      align-items: center;
      gap: 9px;
      flex-wrap: wrap;
    }
    .slider-wrap {
      display: flex;
      align-items: center;
      gap: 9px;
      flex: 1;
      min-width: 280px;
    }
    .speed-indicator {
      font-size: 12px;
      color: #cfe4ff;
      min-width: 44px;
      text-align: center;
    }
    #visionTeamSelect {
      width: 110px;
      padding: 6px 8px;
    }
    button {
      background: #2d6cdf;
      color: #fff;
      border: none;
      border-radius: 5px;
      padding: 7px 12px;
      cursor: pointer;
    }
    button:hover { background: #3a77e7; }
    input[type=range] { flex: 1; }
    input[type=number], select {
      background: #0f1318;
      color: #e8e8e8;
      border: 1px solid #2f3946;
      border-radius: 4px;
      padding: 5px 7px;
      font-size: 12px;
    }
    #mapCanvas {
      width: 100%;
      height: auto;
      aspect-ratio: 1 / 1;
      max-height: calc(100vh - 144px);
      display: block;
      margin: 0 auto;
      background: #111;
      border: 1px solid #414a56;
      border-radius: 8px;
    }
    .legend { font-size: 11px; color: #9ea7b3; }
    .scroll { overflow: auto; min-height: 0; }
    .board-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 2px;
      padding: 2px;
      border-bottom: 1px solid #232a33;
      font-size: 12px;
    }
    .board-row .name { color: #f5f5f5; }
    .board-row .val { color: #8fd3ff; font-weight: bold; }
    .status-row {
      display: grid;
      grid-template-columns: 42px 1fr;
      gap: 9px;
      border-bottom: 1px solid #232a33;
      padding: 8px 5px;
      align-items: center;
      font-size: 12px;
    }
    .avatar {
      width: 34px;
      height: 34px;
      border-radius: 999px;
      background: #2f3946;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #fff;
      font-size: 11px;
      position: relative;
      border: 1px solid #606c7d;
    }
    .respawn-badge {
      position: absolute;
      right: 1px;
      bottom: 1px;
      background: #f44336;
      color: #fff;
      border-radius: 999px;
      padding: 0 4px;
      font-size: 11px;
      font-weight: bold;
      border: 1px solid #ffd5d5;
      white-space: nowrap;
    }
    .hp { color: #7CFC8C; }
    .mp { color: #7BB5FF; }
    .dead { color: #ff7d7d; }
    .small-muted { color: #9ba7b6; font-size: 11px; }
    .settings-modal {
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.55);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 9998;
    }
    .settings-modal.open { display: flex; }
    .settings-body {
      width: min(640px, 92vw);
      max-height: 86vh;
      overflow: auto;
      background: #0f1620;
      border: 1px solid #3d4f66;
      border-radius: 8px;
      padding: 12px;
      box-sizing: border-box;
    }
    .settings-title {
      font-size: 14px;
      color: #d9e6f5;
      margin-bottom: 10px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .settings-grid {
      display: grid;
      grid-template-columns: 1fr 110px;
      gap: 8px 10px;
      align-items: center;
      margin-bottom: 10px;
    }
    .settings-grid label { font-size: 12px; color: #bdd1e7; }
    .settings-hero-list {
      border: 1px solid #2f3946;
      border-radius: 6px;
      padding: 8px;
      background: #0b1119;
      max-height: 220px;
      overflow: auto;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 6px 10px;
      margin-bottom: 10px;
    }
    .settings-hero-item {
      display: flex;
      align-items: center;
      gap: 6px;
      color: #d6e2ef;
      font-size: 12px;
    }
    .settings-actions {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      margin-top: 8px;
    }
    .btn-secondary { background: #3a4d63; }
    .replay-table-wrap {
      border: 1px solid #2f3946;
      border-radius: 6px;
      max-height: 340px;
      overflow: auto;
      background: #0b1119;
    }
    .replay-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    .replay-table th, .replay-table td {
      border-bottom: 1px solid #232a33;
      padding: 8px 6px;
      text-align: left;
      white-space: nowrap;
    }
    .replay-table th {
      color: #bdd1e7;
      position: sticky;
      top: 0;
      background: #111b28;
      z-index: 1;
    }
    .replay-status-ok { color: #6bd38a; font-weight: bold; }
    .replay-status-pending { color: #ffd479; font-weight: bold; }
    .replay-status-bad { color: #ff9a9a; font-weight: bold; }
    .inline-form {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .inline-form input {
      min-width: 240px;
    }
    .muted-line {
      font-size: 12px;
      color: #9ba7b6;
      margin-bottom: 8px;
    }
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
      <div class="meta" id="titleLine">Dota2 回放可视化</div>
      <div class="meta" id="tickLine"></div>
      <div class="controls">
        <div class="control-row">
          <button id="openReplaySelectBtn" style="background:#3a4d63;">录像选择</button>
          <button id="openReplayDownloadBtn" style="background:#3a4d63;">按ID下载录像</button>
          <button id="clearCacheBtn" style="background:#8b1e2d;">清理缓存</button>
          <button id="toggleTrailBtn" style="background:#3a4d63;">轨迹：关</button>
          <button id="openTrailSettingsBtn" style="background:#3a4d63;">轨迹设置</button>
          <button id="toggleHeatmapBtn" style="background:#3a4d63;">热力图：关</button>
          <button id="openHeatmapSettingsBtn" style="background:#3a4d63;">热力图设置</button>
          <button id="toggleVisionBtn" style="background:#3a4d63;">视野：关</button>
          <select id="visionTeamSelect">
            <option value="both">双方视野</option>
            <option value="team1">阵营1视野</option>
            <option value="team2">阵营2视野</option>
          </select>
        </div>
        <div class="control-row">
          <button id="playBtn">播放</button>
          <button id="seekBack10Btn" style="background:#3a4d63;">后退10秒</button>
          <button id="seekForward10Btn" style="background:#3a4d63;">前进10秒</button>
          <button id="speedHalfBtn" style="background:#3a4d63;">0.5x</button>
          <button id="speedDoubleBtn" style="background:#3a4d63;">2x</button>
          <span id="speedIndicator" class="speed-indicator">1x</span>
          <div class="slider-wrap">
            <input id="slider" type="range" min="0" max="1" step="1" value="0" />
            <label for="fpsInput" class="small-muted">刷新率(FPS)</label>
            <input id="fpsInput" type="number" min="1" max="240" step="1" value="30" style="width: 76px;" />
          </div>
        </div>
      </div>
      <canvas id="mapCanvas" width="1200" height="780"></canvas>
      <div class="legend">英雄：绿/红圆点（天辉/夜魇，死亡不显示） | 建筑：基/塔/营/建 | 单位：近/远/车/野 | 资源点：莲/肉/折</div>
    </main>

    <aside class="side right">
      <h3>英雄状态</h3>
      <div class="small-muted" style="margin-bottom: 8px;">显示：HP / MP / 复活倒计时（死亡时）</div>
      <div id="statusList" class="scroll"></div>
    </aside>
  </div>
  <div id="trailSettingsModal" class="settings-modal">
    <div class="settings-body">
      <div class="settings-title">
        <span>轨迹设置</span>
        <button id="trailSettingsCloseBtn" class="btn-secondary">关闭</button>
      </div>
      <div class="settings-grid">
        <label for="trailDensityInput">轨迹密度（每几帧显示一个点）</label>
        <input id="trailDensityInput" type="number" min="1" max="300" step="1" value="12" />
        <label for="trailDotSizeInput">轨迹大小（圆点半径）</label>
        <input id="trailDotSizeInput" type="number" min="1" max="20" step="0.5" value="2.0" />
        <label for="trailLengthSecInput">轨迹长度（最近多少秒）</label>
        <input id="trailLengthSecInput" type="number" min="1" max="300" step="1" value="30" />
      </div>
      <label class="settings-hero-item" style="margin-bottom:8px;">
        <input id="trailFadeEnabledInput" type="checkbox" checked />
        <span>轨迹淡出（旧点逐渐透明到 0）</span>
      </label>
      <div class="small-muted" style="margin-bottom:6px;">英雄筛选（显示哪些英雄轨迹）</div>
      <div id="trailHeroFilterList" class="settings-hero-list"></div>
      <div class="settings-actions">
        <button id="trailSelectAllBtn" class="btn-secondary">全选</button>
        <button id="trailSelectNoneBtn" class="btn-secondary">全不选</button>
      </div>
    </div>
  </div>
  <div id="heatmapSettingsModal" class="settings-modal">
    <div class="settings-body">
      <div class="settings-title">
        <span>热力图设置</span>
        <button id="heatmapSettingsCloseBtn" class="btn-secondary">关闭</button>
      </div>
      <div class="settings-grid">
        <label for="heatmapIntervalSecInput">画圆间隔（秒）</label>
        <input id="heatmapIntervalSecInput" type="number" min="0.1" max="60" step="0.1" value="2.0" />
        <label for="heatmapRadiusInput">圆大小（半径）</label>
        <input id="heatmapRadiusInput" type="number" min="4" max="200" step="1" value="36" />
        <label for="heatmapOpacityInput">不透明度（0~1）</label>
        <input id="heatmapOpacityInput" type="number" min="0.01" max="1" step="0.01" value="0.18" />
        <label for="heatmapWindowSecInput">时间范围（最近多少秒）</label>
        <input id="heatmapWindowSecInput" type="number" min="1" max="300" step="1" value="60" />
      </div>
    </div>
  </div>
  <div id="replaySelectModal" class="settings-modal">
    <div class="settings-body" style="width:min(980px, 95vw);">
      <div class="settings-title">
        <span>录像选择</span>
        <button id="replaySelectCloseBtn" class="btn-secondary">关闭</button>
      </div>
      <div class="muted-line">列表包含录像编号、下载时间、解析状态、解析按钮和播放按钮（仅可播放已解析录像）。</div>
      <div id="replaySelectError" class="muted-line" style="color:#ff9a9a; display:none;"></div>
      <div class="replay-table-wrap">
        <table class="replay-table">
          <thead>
            <tr>
              <th>录像编号</th>
              <th>下载时间</th>
              <th>解析状态</th>
              <th>解析按钮</th>
              <th>播放按钮</th>
            </tr>
          </thead>
          <tbody id="replayTableBody"></tbody>
        </table>
      </div>
    </div>
  </div>
  <div id="replayDownloadModal" class="settings-modal">
    <div class="settings-body" style="width:min(700px, 92vw);">
      <div class="settings-title">
        <span>使用录像 ID 下载录像文件</span>
        <button id="replayDownloadCloseBtn" class="btn-secondary">关闭</button>
      </div>
      <div class="muted-line">输入 Dota2 比赛录像 ID，下载成功后会自动刷新录像列表。</div>
      <div class="inline-form">
        <label for="replayIdInput">录像ID</label>
        <input id="replayIdInput" type="text" placeholder="例如：8781301871" />
        <button id="downloadReplayByIdBtn">下载</button>
      </div>
      <div id="replayDownloadResult" class="muted-line" style="margin-top:10px;"></div>
    </div>
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
    const mapFramePad = 10;
    const mapCoordPad = 16;

    const mapToCanvas = (x, y, bounds, canvas) => {
      const pad = mapCoordPad;
      const nx = bounds.max_x === bounds.min_x ? 0 : (x - bounds.min_x) / (bounds.max_x - bounds.min_x);
      const ny = bounds.max_y === bounds.min_y ? 0 : (y - bounds.min_y) / (bounds.max_y - bounds.min_y);
      const cx = pad + nx * (canvas.width - 2 * pad);
      const cy = pad + (1 - ny) * (canvas.height - 2 * pad);
      return [cx, cy];
    };
    const escapeHtml = (s) =>
      String(s ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");

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

    const entityShortName = (name) => {
      if (!name) return "";
      return String(name)
        .replace("npc_dota_", "")
        .replace("goodguys_", "")
        .replace("badguys_", "")
        .replace("neutral_", "");
    };

    const entityGlyph = (timeline) => {
      const category = timeline.category || "other";
      const subtype = timeline.subtype || "other";
      if (category === "building") {
        if (subtype === "base") return "基";
        if (subtype === "tower") return "塔";
        if (subtype === "barracks") return "营";
        return "建";
      }
      if (category === "creep") {
        if (subtype === "melee") return "近";
        if (subtype === "ranged") return "远";
        if (subtype === "siege") return "车";
        if (subtype === "neutral") return "野";
        return "兵";
      }
      if (category === "lotus_pool") return "莲";
      if (category === "roshan") return "肉";
      if (category === "tormentor") return "折";
      return "?";
    };

    const entityColors = (timeline) => {
      const category = timeline.category || "other";
      const subtype = timeline.subtype || "other";
      if (category === "building") {
        if (subtype === "base") return { fill: "#f9a825", stroke: "#fff3c4" };
        if (subtype === "tower") return { fill: "#ef6c00", stroke: "#ffe0b2" };
        if (subtype === "barracks") return { fill: "#8d6e63", stroke: "#d7ccc8" };
        return { fill: "#546e7a", stroke: "#cfd8dc" };
      }
      if (category === "creep") {
        if (subtype === "melee") return { fill: "#78909c", stroke: "#eceff1" };
        if (subtype === "ranged") return { fill: "#26a69a", stroke: "#e0f2f1" };
        if (subtype === "siege") return { fill: "#607d8b", stroke: "#cfd8dc" };
        if (subtype === "neutral") return { fill: "#8e24aa", stroke: "#f3e5f5" };
        return { fill: "#5c6bc0", stroke: "#e8eaf6" };
      }
      if (category === "lotus_pool") return { fill: "#00acc1", stroke: "#e0f7fa" };
      if (category === "roshan") return { fill: "#6d4c41", stroke: "#efebe9" };
      if (category === "tormentor") return { fill: "#6a1b9a", stroke: "#f3e5f5" };
      return { fill: "#455a64", stroke: "#eceff1" };
    };

    const drawEntityGlyph = (ctx2, cx, cy, timeline) => {
      const category = timeline.category || "other";
      const subtype = timeline.subtype || "other";
      const colors = entityColors(timeline);
      const glyph = entityGlyph(timeline);
      const radius = category === "roshan" || category === "tormentor" ? 9 : 7;

      ctx2.strokeStyle = colors.stroke;
      ctx2.fillStyle = colors.fill;
      ctx2.lineWidth = 1.2;
      ctx2.beginPath();
      if (category === "building" && subtype === "tower") {
        ctx2.moveTo(cx, cy - radius);
        ctx2.lineTo(cx - radius, cy + radius);
        ctx2.lineTo(cx + radius, cy + radius);
        ctx2.closePath();
      } else if (category === "building" && subtype === "barracks") {
        ctx2.moveTo(cx, cy - radius);
        ctx2.lineTo(cx - radius, cy);
        ctx2.lineTo(cx, cy + radius);
        ctx2.lineTo(cx + radius, cy);
        ctx2.closePath();
      } else if (category === "creep" && subtype === "siege") {
        ctx2.rect(cx - radius, cy - radius * 0.7, radius * 2, radius * 1.4);
      } else {
        ctx2.arc(cx, cy, radius, 0, Math.PI * 2);
      }
      ctx2.fill();
      ctx2.stroke();

      ctx2.fillStyle = "#ffffff";
      ctx2.font = "10px Arial";
      ctx2.textAlign = "center";
      ctx2.textBaseline = "middle";
      ctx2.fillText(glyph, cx, cy);
    };

    const renderWorldEntities = (tick) => {
      for (const timeline of (data.entity_timelines || [])) {
        const st = stateAtTick(timeline, tick);
        // 地图层只绘制激活对象；未激活对象可在下方调试表查看。
        if (!st || st.x === null || st.y === null || !st.active) continue;
        if (!isVisibleByVision(st.x, st.y, tick)) continue;
        const [cx, cy] = mapToCanvas(st.x, st.y, data.map_bounds, canvas);
        drawEntityGlyph(ctx, cx, cy, timeline);

        if (timeline.category === "roshan" || timeline.category === "tormentor" || timeline.category === "lotus_pool") {
          ctx.fillStyle = "#d9e4f0";
          ctx.font = "10px Arial";
          ctx.textAlign = "left";
          ctx.textBaseline = "bottom";
          ctx.fillText(entityShortName(timeline.entity_name), cx + 10, cy - 2);
        }
      }
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

    const titleLine = document.getElementById("titleLine");
    const tickLine = document.getElementById("tickLine");
    const playBtn = document.getElementById("playBtn");
    const seekBack10Btn = document.getElementById("seekBack10Btn");
    const seekForward10Btn = document.getElementById("seekForward10Btn");
    const speedHalfBtn = document.getElementById("speedHalfBtn");
    const speedDoubleBtn = document.getElementById("speedDoubleBtn");
    const speedIndicator = document.getElementById("speedIndicator");
    const slider = document.getElementById("slider");
    const clearCacheBtn = document.getElementById("clearCacheBtn");
    const openReplaySelectBtn = document.getElementById("openReplaySelectBtn");
    const openReplayDownloadBtn = document.getElementById("openReplayDownloadBtn");
    const toggleTrailBtn = document.getElementById("toggleTrailBtn");
    const openTrailSettingsBtn = document.getElementById("openTrailSettingsBtn");
    const toggleHeatmapBtn = document.getElementById("toggleHeatmapBtn");
    const openHeatmapSettingsBtn = document.getElementById("openHeatmapSettingsBtn");
    const toggleVisionBtn = document.getElementById("toggleVisionBtn");
    const visionTeamSelect = document.getElementById("visionTeamSelect");
    const boardMetric = document.getElementById("boardMetric");
    const boardList = document.getElementById("boardList");
    const statusList = document.getElementById("statusList");
    const fpsInput = document.getElementById("fpsInput");
    const canvas = document.getElementById("mapCanvas");
    const ctx = canvas.getContext("2d");
    const trailSettingsModal = document.getElementById("trailSettingsModal");
    const trailSettingsCloseBtn = document.getElementById("trailSettingsCloseBtn");
    const trailDensityInput = document.getElementById("trailDensityInput");
    const trailDotSizeInput = document.getElementById("trailDotSizeInput");
    const trailLengthSecInput = document.getElementById("trailLengthSecInput");
    const trailFadeEnabledInput = document.getElementById("trailFadeEnabledInput");
    const trailHeroFilterList = document.getElementById("trailHeroFilterList");
    const trailSelectAllBtn = document.getElementById("trailSelectAllBtn");
    const trailSelectNoneBtn = document.getElementById("trailSelectNoneBtn");
    const heatmapSettingsModal = document.getElementById("heatmapSettingsModal");
    const heatmapSettingsCloseBtn = document.getElementById("heatmapSettingsCloseBtn");
    const heatmapIntervalSecInput = document.getElementById("heatmapIntervalSecInput");
    const heatmapRadiusInput = document.getElementById("heatmapRadiusInput");
    const heatmapOpacityInput = document.getElementById("heatmapOpacityInput");
    const heatmapWindowSecInput = document.getElementById("heatmapWindowSecInput");
    const replaySelectModal = document.getElementById("replaySelectModal");
    const replaySelectCloseBtn = document.getElementById("replaySelectCloseBtn");
    const replayTableBody = document.getElementById("replayTableBody");
    const replaySelectError = document.getElementById("replaySelectError");
    const replayDownloadModal = document.getElementById("replayDownloadModal");
    const replayDownloadCloseBtn = document.getElementById("replayDownloadCloseBtn");
    const replayIdInput = document.getElementById("replayIdInput");
    const downloadReplayByIdBtn = document.getElementById("downloadReplayByIdBtn");
    const replayDownloadResult = document.getElementById("replayDownloadResult");
    // debug+DSR-MAPDBG-01: 统一调试 ID，用于定位“页面打开到地图可见”的耗时链路。
    const debugId = "debug+DSR-MAPDBG-01";
    const pageBootMs = performance.now();
    const debugLog = (stage, extra = null) => {
      const elapsed = (performance.now() - pageBootMs).toFixed(1);
      if (extra === null) {
        console.debug(`[${debugId}] ${stage} | +${elapsed}ms`);
      } else {
        console.debug(`[${debugId}] ${stage} | +${elapsed}ms`, extra);
      }
    };
    const mapFrameRatio = 1;
    // debug+DSR-MAPDBG-01: 固定裁剪参数（本轮调试确认值）。
    const mapCropConfig = {
      loadWidth: 1045,  // 载入宽度（裁剪框宽）
      loadHeight: 1070, // 载入高度（裁剪框高）
      offsetX: 69,      // 横偏移：裁剪框左下角到原图左下角的 x 距离
      offsetY: 65,      // 纵偏移：裁剪框左下角到原图左下角的 y 距离
    };
    const heroTrailSettings = {
      enabled: false,
      sampleEveryTicks: 12,
      dotRadius: 2.0,
      durationSec: 30,
      fadeOut: true,
      selectedHeroes: new Set(),
    };
    let heroSelectionInitialized = false;
    const heatmapSettings = {
      enabled: false,
      intervalSec: 2.0,
      radius: 36,
      opacity: 0.18,
      durationSec: 60,
    };
    const mapView = {
      zoom: 1.0,
      minZoom: 1.0,
      maxZoom: 4.0,
      panX: 0.0,
      panY: 0.0,
      dragging: false,
      lastX: 0.0,
      lastY: 0.0,
    };
    const visionSettings = {
      enabled: false,
      mode: "both",
      heroVisionRadius: 1600,
      treeRadius: 70,
      treeBlockers: [],
      team1: 2,
      team2: 3,
    };

    let data = null;
    let playing = false;
    let timer = null;
    let currentTick = 0;
    let currentTickFloat = 0;
    let playbackAnchorRealMs = 0;
    let playbackAnchorTick = 0;
    let playbackSpeed = 1.0;
    let hasLoggedFirstMapRender = false;
    let hasLoggedMapWait = false;
    let hasLoggedCropRect = false;
    let hasLoggedResize = false;
    let replayListCache = [];
    const mapBackgroundImage = new Image();
    let mapBackgroundLoaded = false;
    // debug+DSR-MAPDBG-01: 底图加载开始与结束日志。
    debugLog("map-image-load-start", { src: "/assets/maps/map_full.png" });
    mapBackgroundImage.onload = () => {
      mapBackgroundLoaded = true;
      debugLog("map-image-load-success", {
        naturalWidth: mapBackgroundImage.naturalWidth,
        naturalHeight: mapBackgroundImage.naturalHeight,
      });
      if (data) render(currentTick);
    };
    mapBackgroundImage.onerror = () => {
      mapBackgroundLoaded = false;
      debugLog("map-image-load-failed");
    };
    mapBackgroundImage.src = "/assets/maps/map_full.png";

    const resizeCanvasToMapAspect = () => {
      const availableHeight = Math.max(window.innerHeight - 144, 1);
      const containerWidth = canvas.parentElement ? canvas.parentElement.clientWidth : 0;
      const availableWidth = Math.max(containerWidth - 20, 1);

      // 双向约束：
      // 1) 宽度不超过可用宽度，且不超过高度 / (h/w)
      // 2) 高度不超过可用高度，且不超过宽度 * (h/w)
      const mapHeightWidthRatio = 1 / mapFrameRatio;
      const targetWidth = Math.min(availableWidth, availableHeight / mapHeightWidthRatio);
      const targetHeight = Math.min(availableHeight, availableWidth * mapHeightWidthRatio);

      canvas.width = Math.max(Math.round(targetWidth), 1);
      canvas.height = Math.max(Math.round(targetHeight), 1);
      canvas.style.width = `${Math.max(Math.round(targetWidth), 1)}px`;
      canvas.style.height = `${Math.max(Math.round(targetHeight), 1)}px`;
      // debug+DSR-MAPDBG-01: 记录尺寸约束首次命中值，确认地图未被侧栏遮挡。
      if (!hasLoggedResize) {
        hasLoggedResize = true;
        debugLog("map-size-first-computed", {
          availableWidth,
          availableHeight,
          targetWidth: canvas.width,
          targetHeight: canvas.height,
        });
      }
    };

    const getMapCropRect = (img) => {
      const cropWidth = Math.max(1, Math.min(Math.round(mapCropConfig.loadWidth), img.width));
      const cropHeight = Math.max(1, Math.min(Math.round(mapCropConfig.loadHeight), img.height));
      const maxOffsetX = Math.max(img.width - cropWidth, 0);
      const maxOffsetY = Math.max(img.height - cropHeight, 0);
      const offsetX = Math.max(0, Math.min(Math.round(mapCropConfig.offsetX), maxOffsetX));
      const offsetYFromBottom = Math.max(0, Math.min(Math.round(mapCropConfig.offsetY), maxOffsetY));
      const sourceX = offsetX;
      const sourceY = img.height - offsetYFromBottom - cropHeight;
      // debug+DSR-MAPDBG-01: 记录首帧裁剪框参数，确认偏移和载入范围。
      if (!hasLoggedCropRect) {
        hasLoggedCropRect = true;
        debugLog("map-crop-first-computed", {
          sourceX,
          sourceY,
          cropWidth,
          cropHeight,
          imageWidth: img.width,
          imageHeight: img.height,
        });
      }
      return { sourceX, sourceY, cropWidth, cropHeight };
    };

    const applyMapViewTransform = () => {
      const centerX = canvas.width / 2;
      const centerY = canvas.height / 2;
      ctx.translate(centerX + mapView.panX, centerY + mapView.panY);
      ctx.scale(mapView.zoom, mapView.zoom);
      ctx.translate(-centerX, -centerY);
    };

    const screenToPreView = (screenX, screenY) => {
      const centerX = canvas.width / 2;
      const centerY = canvas.height / 2;
      return {
        x: (screenX - centerX - mapView.panX) / mapView.zoom + centerX,
        y: (screenY - centerY - mapView.panY) / mapView.zoom + centerY,
      };
    };

    const updateVisionTeamOptions = () => {
      const teams = [...new Set((data?.player_timelines || []).map((x) => Number(x.team)))].sort((a, b) => a - b);
      visionSettings.team1 = teams[0] ?? 2;
      visionSettings.team2 = teams[1] ?? teams[0] ?? 3;
      const team1Label = `阵营1视野(T${visionSettings.team1})`;
      const team2Label = `阵营2视野(T${visionSettings.team2})`;
      visionTeamSelect.innerHTML = `
        <option value="both">双方视野</option>
        <option value="team1">${team1Label}</option>
        <option value="team2">${team2Label}</option>
      `;
      visionTeamSelect.value = visionSettings.mode;
      visionTeamSelect.disabled = !visionSettings.enabled;
    };

    const initTreeBlockers = () => {
      if (!data) return;
      const trees = [];
      const spanX = data.map_bounds.max_x - data.map_bounds.min_x;
      const spanY = data.map_bounds.max_y - data.map_bounds.min_y;
      const cols = 28;
      const rows = 28;
      for (let r = 2; r < rows - 2; r += 1) {
        for (let c = 2; c < cols - 2; c += 1) {
          // 规则化伪随机分布，避免每次刷新树位变化。
          const seed = (r * 73856093) ^ (c * 19349663);
          if ((seed % 100) > 22) continue;
          const jx = ((seed % 7) - 3) / 7;
          const jy = (((seed >> 3) % 7) - 3) / 7;
          const nx = (c + 0.5 + jx * 0.3) / cols;
          const ny = (r + 0.5 + jy * 0.3) / rows;
          trees.push({
            x: data.map_bounds.min_x + nx * spanX,
            y: data.map_bounds.min_y + ny * spanY,
          });
        }
      }
      visionSettings.treeBlockers = trees;
    };

    const pointSegmentDistSq = (px, py, x1, y1, x2, y2) => {
      const vx = x2 - x1;
      const vy = y2 - y1;
      const wx = px - x1;
      const wy = py - y1;
      const c1 = vx * wx + vy * wy;
      if (c1 <= 0) return (px - x1) ** 2 + (py - y1) ** 2;
      const c2 = vx * vx + vy * vy;
      if (c2 <= c1) return (px - x2) ** 2 + (py - y2) ** 2;
      const t = c1 / c2;
      const projX = x1 + t * vx;
      const projY = y1 + t * vy;
      return (px - projX) ** 2 + (py - projY) ** 2;
    };

    const isSightBlockedByTree = (sx, sy, tx, ty) => {
      const radiusSq = visionSettings.treeRadius * visionSettings.treeRadius;
      for (const tree of visionSettings.treeBlockers) {
        if (pointSegmentDistSq(tree.x, tree.y, sx, sy, tx, ty) <= radiusSq) {
          return true;
        }
      }
      return false;
    };

    const getVisionSourceTimelines = () => {
      if (!data) return [];
      if (visionSettings.mode === "team1") {
        return data.player_timelines.filter((x) => Number(x.team) === visionSettings.team1);
      }
      if (visionSettings.mode === "team2") {
        return data.player_timelines.filter((x) => Number(x.team) === visionSettings.team2);
      }
      return data.player_timelines;
    };

    const isVisibleByVision = (x, y, tick) => {
      if (!visionSettings.enabled) return true;
      const sources = getVisionSourceTimelines();
      const radiusSq = visionSettings.heroVisionRadius * visionSettings.heroVisionRadius;
      for (const source of sources) {
        const st = stateAtTick(source, tick);
        if (!st || st.x === null || st.y === null || st.hp <= 0) continue;
        const death = deathInfoAtTick(source, tick);
        if (death.is_dead) continue;
        const dx = x - st.x;
        const dy = y - st.y;
        if ((dx * dx + dy * dy) > radiusSq) continue;
        if (!isSightBlockedByTree(st.x, st.y, x, y)) return true;
      }
      return false;
    };

    const renderMap = (tick) => {
      resizeCanvasToMapAspect();
      ctx.fillStyle = "#111";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.save();
      applyMapViewTransform();
      if (mapBackgroundLoaded) {
        const crop = getMapCropRect(mapBackgroundImage);
        ctx.save();
        ctx.globalAlpha = 0.92;
        ctx.drawImage(
          mapBackgroundImage,
          crop.sourceX,
          crop.sourceY,
          crop.cropWidth,
          crop.cropHeight,
          mapFramePad,
          mapFramePad,
          canvas.width - 2 * mapFramePad,
          canvas.height - 2 * mapFramePad
        );
        ctx.restore();
      } else if (!hasLoggedMapWait) {
        hasLoggedMapWait = true;
        // debug+DSR-MAPDBG-01: 区分“数据到了但底图尚未加载”的等待状态。
        debugLog("map-render-waiting-image");
      }
      // debug+DSR-MAPDBG-01: 地图首帧渲染完成时刻。
      if (!hasLoggedFirstMapRender) {
        hasLoggedFirstMapRender = true;
        debugLog("map-first-render-done", { tick, mapBackgroundLoaded });
      }
      ctx.strokeStyle = "#666";
      ctx.lineWidth = 2;
      ctx.strokeRect(
        mapFramePad,
        mapFramePad,
        canvas.width - 2 * mapFramePad,
        canvas.height - 2 * mapFramePad
      );
      ctx.fillStyle = "#ccc";
      ctx.font = "14px Arial";
      ctx.fillText(
        mapBackgroundLoaded ? "地图（底图 + 归一化坐标）" : "地图（归一化坐标）",
        mapFramePad + 10,
        mapFramePad + 16
      );
      renderWorldEntities(tick);
      renderHeroHeatmap(tick);
      renderHeroTrails(tick);

      for (const timeline of data.player_timelines) {
        const st = stateAtTick(timeline, tick);
        if (!st || st.x === null || st.y === null) continue;
        const death = deathInfoAtTick(timeline, tick);
        if (death.is_dead || st.hp <= 0) continue;
        if (!isVisibleByVision(st.x, st.y, tick)) continue;

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
      ctx.restore();
    };

    const ensureHeroSelectionInitialized = () => {
      if (!data || heroSelectionInitialized) return;
      heroTrailSettings.selectedHeroes.clear();
      for (const timeline of data.player_timelines) {
        heroTrailSettings.selectedHeroes.add(timeline.hero_name);
      }
      heroSelectionInitialized = true;
    };

    const computeRecentHeroPoints = (timeline, tick, durationSec, everyTicks) => {
      const out = [];
      const startTick = Math.max(0, tick - Math.round(durationSec * data.tick_rate));
      const step = Math.max(1, everyTicks);
      const endAlignedTick = Math.floor(tick / step) * step;
      for (let alignedTick = endAlignedTick; alignedTick >= startTick; alignedTick -= step) {
        const idx = upperBound(timeline.ticks, alignedTick) - 1;
        if (idx < 0) continue;
        const st = timeline.states[idx];
        if (!st || st.x === null || st.y === null) continue;
        out.push({ tick: alignedTick, x: st.x, y: st.y });
      }
      return out;
    };

    const renderHeroTrails = (tick) => {
      if (!data || !heroTrailSettings.enabled) return;
      const durationTicks = Math.max(1, Math.round(heroTrailSettings.durationSec * data.tick_rate));
      for (const timeline of data.player_timelines) {
        if (!heroTrailSettings.selectedHeroes.has(timeline.hero_name)) continue;
        const pts = computeRecentHeroPoints(
          timeline,
          tick,
          heroTrailSettings.durationSec,
          heroTrailSettings.sampleEveryTicks
        );
        for (const pt of pts) {
          if (!isVisibleByVision(pt.x, pt.y, tick)) continue;
          const [cx, cy] = mapToCanvas(pt.x, pt.y, data.map_bounds, canvas);
          let alpha = 0.85;
          if (heroTrailSettings.fadeOut) {
            const age = Math.max(0, tick - pt.tick);
            const factor = 1 - age / durationTicks;
            alpha = Math.max(0, Math.min(1, factor)) * 0.95;
          }
          ctx.save();
          ctx.globalAlpha = alpha;
          ctx.beginPath();
          ctx.fillStyle = timeline.team === 2 ? "#63d471" : "#ff7668";
          ctx.arc(cx, cy, Math.max(1, heroTrailSettings.dotRadius), 0, Math.PI * 2);
          ctx.fill();
          ctx.restore();
        }
      }
    };

    const renderHeroHeatmap = (tick) => {
      if (!data || !heatmapSettings.enabled) return;
      const intervalTicks = Math.max(1, Math.round(heatmapSettings.intervalSec * data.tick_rate));
      for (const timeline of data.player_timelines) {
        const pts = computeRecentHeroPoints(
          timeline,
          tick,
          heatmapSettings.durationSec,
          intervalTicks
        );
        for (const pt of pts) {
          if (!isVisibleByVision(pt.x, pt.y, tick)) continue;
          const [cx, cy] = mapToCanvas(pt.x, pt.y, data.map_bounds, canvas);
          ctx.save();
          ctx.globalAlpha = Math.max(0.01, Math.min(1, heatmapSettings.opacity));
          ctx.beginPath();
          ctx.fillStyle = timeline.team === 2 ? "#73bf69" : "#e57373";
          ctx.arc(cx, cy, Math.max(2, heatmapSettings.radius), 0, Math.PI * 2);
          ctx.fill();
          ctx.restore();
        }
      }
    };

    const updateTrailToggleText = () => {
      toggleTrailBtn.textContent = `轨迹：${heroTrailSettings.enabled ? "开" : "关"}`;
    };
    const updateHeatmapToggleText = () => {
      toggleHeatmapBtn.textContent = `热力图：${heatmapSettings.enabled ? "开" : "关"}`;
    };
    const updateVisionToggleText = () => {
      toggleVisionBtn.textContent = `视野：${visionSettings.enabled ? "开" : "关"}`;
      visionTeamSelect.disabled = !visionSettings.enabled;
    };

    const rebuildTrailHeroFilterUI = () => {
      if (!data) return;
      const html = data.player_timelines.map((timeline) => {
        const checked = heroTrailSettings.selectedHeroes.has(timeline.hero_name) ? "checked" : "";
        const label = `${shortHeroName(timeline.hero_name)} (${timeline.player_name || shortHeroName(timeline.hero_name)})`;
        return `
          <label class="settings-hero-item">
            <input type="checkbox" data-hero-name="${escapeHtml(timeline.hero_name)}" ${checked} />
            <span>${escapeHtml(label)}</span>
          </label>
        `;
      }).join("");
      trailHeroFilterList.innerHTML = html;
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
          <div class="name">${row.hero}<div class="small-muted">(${row.name})</div></div>
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
        const respawnBadge = death.is_dead ? `<span class="respawn-badge">${respawnSec}</span>` : "";
        return `
          <div class="status-row">
            <div class="avatar">
              ${heroAvatarText(timeline.hero_name)}
              ${respawnBadge}
            </div>
            <div>
              <div><strong>${shortHeroName(timeline.hero_name)}</strong> <span class="small-muted">(${timeline.player_name || shortHeroName(timeline.hero_name)})</span></div>
              <div class="hp">HP: ${hpText}</div>
              <div class="mp">MP: ${manaText}</div>
            </div>
          </div>
        `;
      }).join("");
    };

    const render = (tick) => {
      tick = Math.max(0, Math.min(data.game_end_tick, Math.round(tick)));
      currentTick = tick;
      slider.value = String(tick);

      const gameSeconds = (tick - data.game_start_tick) / data.tick_rate;
      tickLine.textContent = `Tick: ${tick} | 游戏时间: ${formatGameTime(gameSeconds)} | 游戏开始 tick: ${data.game_start_tick}`;
      renderMap(tick);
      renderBoard(tick);
      renderStatus(tick);
    };

    const renderFromFloat = (tickFloat) => {
      const clamped = Math.max(0, Math.min(data.game_end_tick, tickFloat));
      currentTickFloat = clamped;
      render(clamped);
    };

    const stopPlayback = () => {
      playing = false;
      playBtn.textContent = "播放";
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
    };

    const updateSpeedUI = () => {
      speedIndicator.textContent = `${playbackSpeed}x`;
      speedHalfBtn.style.background = playbackSpeed === 0.5 ? "#2d6cdf" : "#3a4d63";
      speedDoubleBtn.style.background = playbackSpeed === 2.0 ? "#2d6cdf" : "#3a4d63";
    };

    const setPlaybackSpeed = (speed) => {
      playbackSpeed = speed;
      updateSpeedUI();
      if (playing) {
        playbackAnchorRealMs = performance.now();
        playbackAnchorTick = currentTickFloat;
      }
    };

    const seekBySeconds = (deltaSec) => {
      if (!data) return;
      const deltaTick = deltaSec * data.tick_rate;
      renderFromFloat(currentTickFloat + deltaTick);
      if (playing) {
        playbackAnchorRealMs = performance.now();
        playbackAnchorTick = currentTickFloat;
      }
    };

    const startPlayback = () => {
      const fps = Math.max(1, Number(fpsInput.value) || data.playback_fps || 30);
      fpsInput.value = String(Math.round(fps));
      playing = true;
      playBtn.textContent = "暂停";
      playbackAnchorRealMs = performance.now();
      playbackAnchorTick = currentTickFloat;
      const delay = Math.max(Math.round(1000 / fps), 1);
      timer = setInterval(() => {
        const elapsedSec = (performance.now() - playbackAnchorRealMs) / 1000;
        const targetTickFloat = playbackAnchorTick + elapsedSec * data.tick_rate * playbackSpeed;
        if (targetTickFloat >= data.game_end_tick) {
          renderFromFloat(data.game_end_tick);
          stopPlayback();
          return;
        }
        renderFromFloat(targetTickFloat);
      }, delay);
    };

    const formatDownloadTime = (raw) => {
      if (!raw) return "-";
      try {
        return new Date(raw).toLocaleString("zh-CN", { hour12: false });
      } catch (err) {
        return raw;
      }
    };

    const statusClass = (parsed, parse_error) => {
      if (parsed) return "replay-status-ok";
      if (parse_error) return "replay-status-bad";
      return "replay-status-pending";
    };

    const statusText = (parsed, parse_error) => {
      if (parsed) return "已解析";
      if (parse_error) return "解析失败";
      return "未解析";
    };

    const loadReplayRecord = async (record) => {
      if (!record || !record.dem_path) return;
      try {
        const res = await fetch("/load_replay", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ dem_path: record.dem_path }),
        });
        const obj = await res.json();
        if (!res.ok || !obj || !obj.ok) {
          throw new Error((obj && obj.error) || `HTTP ${res.status}`);
        }
        data = obj.payload;
        titleLine.textContent = `Dota2 回放可视化（Match ${data.match_id}）`;
        slider.min = "0";
        slider.max = String(data.game_end_tick);
        slider.value = "0";
        fpsInput.value = String(data.playback_fps || 30);
        stopPlayback();
        currentTick = 0;
        currentTickFloat = 0;
        initTreeBlockers();
        updateVisionTeamOptions();
        ensureHeroSelectionInitialized();
        rebuildTrailHeroFilterUI();
        renderFromFloat(0);
      } catch (err) {
        alert(`加载录像失败：${String(err)}`);
      }
    };

    const parseReplay = async (record) => {
      if (!record || !record.dem_path) return;
      try {
        const res = await fetch("/parse_replay", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ dem_path: record.dem_path }),
        });
        const obj = await res.json();
        if (!res.ok || !obj || !obj.ok) {
          throw new Error((obj && obj.error) || `HTTP ${res.status}`);
        }
        await loadReplayList();
      } catch (err) {
        alert(`解析失败：${String(err)}`);
      }
    };

    const renderReplayTable = () => {
      replayTableBody.innerHTML = replayListCache.map((record, idx) => {
        const parsed = Boolean(record.parsed);
        const statusCls = statusClass(parsed, record.parse_error);
        const parseDisabled = parsed ? "disabled" : "";
        const playDisabled = parsed ? "" : "disabled";
        return `
          <tr>
            <td>${escapeHtml(record.replay_id || `#${idx + 1}`)}</td>
            <td>${escapeHtml(formatDownloadTime(record.downloaded_at))}</td>
            <td><span class="${statusCls}">${escapeHtml(statusText(parsed, record.parse_error))}</span></td>
            <td><button data-action="parse" data-idx="${idx}" ${parseDisabled}>解析</button></td>
            <td><button data-action="play" data-idx="${idx}" ${playDisabled}>播放</button></td>
          </tr>
        `;
      }).join("");
    };

    const loadReplayList = async () => {
      replaySelectError.style.display = "none";
      replaySelectError.textContent = "";
      try {
        const res = await fetch("/replays");
        const obj = await res.json();
        if (!res.ok || !obj || !Array.isArray(obj.replays)) {
          throw new Error(`HTTP ${res.status}`);
        }
        replayListCache = obj.replays;
        const currentDemPath = String((data && data.dem_path) || "");
        for (const record of replayListCache) {
          if (String(record.dem_path || "") === currentDemPath) {
            record.parsed = true;
          }
        }
        renderReplayTable();
      } catch (err) {
        replayListCache = [];
        replayTableBody.innerHTML = "";
        replaySelectError.style.display = "block";
        replaySelectError.textContent = `加载录像列表失败：${String(err)}`;
      }
    };

    playBtn.addEventListener("click", () => {
      if (!data) return;
      if (playing) stopPlayback();
      else startPlayback();
    });
    openReplaySelectBtn.addEventListener("click", async () => {
      replaySelectModal.classList.add("open");
      await loadReplayList();
    });
    replaySelectCloseBtn.addEventListener("click", () => replaySelectModal.classList.remove("open"));
    replaySelectModal.addEventListener("click", (e) => {
      if (e.target === replaySelectModal) replaySelectModal.classList.remove("open");
    });
    replayTableBody.addEventListener("click", async (e) => {
      const target = e.target;
      if (!(target instanceof HTMLButtonElement)) return;
      const action = target.getAttribute("data-action");
      const idx = Number(target.getAttribute("data-idx"));
      if (!Number.isFinite(idx) || idx < 0 || idx >= replayListCache.length) return;
      const record = replayListCache[idx];
      if (action === "parse") {
        await parseReplay(record);
      } else if (action === "play") {
        if (!record.parsed) {
          alert("仅可播放已解析的录像。");
          return;
        }
        await loadReplayRecord(record);
        replaySelectModal.classList.remove("open");
      }
    });
    openReplayDownloadBtn.addEventListener("click", () => {
      replayDownloadResult.textContent = "";
      replayDownloadModal.classList.add("open");
    });
    replayDownloadCloseBtn.addEventListener("click", () => replayDownloadModal.classList.remove("open"));
    replayDownloadModal.addEventListener("click", (e) => {
      if (e.target === replayDownloadModal) replayDownloadModal.classList.remove("open");
    });
    downloadReplayByIdBtn.addEventListener("click", async () => {
      const replayId = String(replayIdInput.value || "").trim();
      if (!/^[0-9]{6,}$/.test(replayId)) {
        replayDownloadResult.style.color = "#ff9a9a";
        replayDownloadResult.textContent = "请输入合法的数字录像ID。";
        return;
      }
      replayDownloadResult.style.color = "#9ba7b6";
      replayDownloadResult.textContent = "正在下载，请稍候...";
      try {
        const res = await fetch("/download_replay_by_id", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ replay_id: replayId }),
        });
        const obj = await res.json();
        if (!res.ok || !obj || !obj.ok) {
          throw new Error((obj && obj.error) || `HTTP ${res.status}`);
        }
        replayDownloadResult.style.color = "#6bd38a";
        replayDownloadResult.textContent = `下载成功：${obj.file_path}`;
        await loadReplayList();
      } catch (err) {
        replayDownloadResult.style.color = "#ff9a9a";
        replayDownloadResult.textContent = `下载失败：${String(err)}`;
      }
    });
    seekBack10Btn.addEventListener("click", () => seekBySeconds(-10));
    seekForward10Btn.addEventListener("click", () => seekBySeconds(10));
    speedHalfBtn.addEventListener("click", () => setPlaybackSpeed(0.5));
    speedDoubleBtn.addEventListener("click", () => setPlaybackSpeed(2.0));
    toggleVisionBtn.addEventListener("click", () => {
      visionSettings.enabled = !visionSettings.enabled;
      updateVisionToggleText();
      if (data) render(currentTick);
    });
    visionTeamSelect.addEventListener("change", () => {
      visionSettings.mode = visionTeamSelect.value;
      if (data) render(currentTick);
    });

    slider.addEventListener("input", (e) => {
      if (!data) return;
      renderFromFloat(Number(e.target.value));
      if (playing) {
        playbackAnchorRealMs = performance.now();
        playbackAnchorTick = currentTickFloat;
      }
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
    toggleTrailBtn.addEventListener("click", () => {
      heroTrailSettings.enabled = !heroTrailSettings.enabled;
      updateTrailToggleText();
      if (data) render(currentTick);
    });
    toggleHeatmapBtn.addEventListener("click", () => {
      heatmapSettings.enabled = !heatmapSettings.enabled;
      updateHeatmapToggleText();
      if (data) render(currentTick);
    });
    openTrailSettingsBtn.addEventListener("click", () => {
      trailSettingsModal.classList.add("open");
      if (data) rebuildTrailHeroFilterUI();
    });
    trailSettingsCloseBtn.addEventListener("click", () => trailSettingsModal.classList.remove("open"));
    trailSettingsModal.addEventListener("click", (e) => {
      if (e.target === trailSettingsModal) trailSettingsModal.classList.remove("open");
    });
    openHeatmapSettingsBtn.addEventListener("click", () => heatmapSettingsModal.classList.add("open"));
    heatmapSettingsCloseBtn.addEventListener("click", () => heatmapSettingsModal.classList.remove("open"));
    heatmapSettingsModal.addEventListener("click", (e) => {
      if (e.target === heatmapSettingsModal) heatmapSettingsModal.classList.remove("open");
    });
    trailHeroFilterList.addEventListener("change", (e) => {
      const target = e.target;
      if (!(target instanceof HTMLInputElement)) return;
      const heroName = target.getAttribute("data-hero-name");
      if (!heroName) return;
      if (target.checked) heroTrailSettings.selectedHeroes.add(heroName);
      else heroTrailSettings.selectedHeroes.delete(heroName);
      if (data) render(currentTick);
    });
    trailSelectAllBtn.addEventListener("click", () => {
      if (!data) return;
      heroTrailSettings.selectedHeroes = new Set(data.player_timelines.map((x) => x.hero_name));
      rebuildTrailHeroFilterUI();
      render(currentTick);
    });
    trailSelectNoneBtn.addEventListener("click", () => {
      heroTrailSettings.selectedHeroes.clear();
      rebuildTrailHeroFilterUI();
      if (data) render(currentTick);
    });
    const applyTrailNumberInput = () => {
      heroTrailSettings.sampleEveryTicks = Math.max(1, Math.min(300, Number(trailDensityInput.value) || 12));
      heroTrailSettings.dotRadius = Math.max(1, Math.min(20, Number(trailDotSizeInput.value) || 2.0));
      heroTrailSettings.durationSec = Math.max(1, Math.min(300, Number(trailLengthSecInput.value) || 30));
      heroTrailSettings.fadeOut = Boolean(trailFadeEnabledInput.checked);
      trailDensityInput.value = String(Math.round(heroTrailSettings.sampleEveryTicks));
      trailDotSizeInput.value = String(Number(heroTrailSettings.dotRadius.toFixed(1)));
      trailLengthSecInput.value = String(Math.round(heroTrailSettings.durationSec));
      if (data) render(currentTick);
    };
    trailDensityInput.addEventListener("change", applyTrailNumberInput);
    trailDotSizeInput.addEventListener("change", applyTrailNumberInput);
    trailLengthSecInput.addEventListener("change", applyTrailNumberInput);
    trailFadeEnabledInput.addEventListener("change", applyTrailNumberInput);
    const applyHeatmapNumberInput = () => {
      heatmapSettings.intervalSec = Math.max(0.1, Math.min(60, Number(heatmapIntervalSecInput.value) || 2.0));
      heatmapSettings.radius = Math.max(4, Math.min(200, Number(heatmapRadiusInput.value) || 36));
      heatmapSettings.opacity = Math.max(0.01, Math.min(1, Number(heatmapOpacityInput.value) || 0.18));
      heatmapSettings.durationSec = Math.max(1, Math.min(300, Number(heatmapWindowSecInput.value) || 60));
      heatmapIntervalSecInput.value = String(Number(heatmapSettings.intervalSec.toFixed(1)));
      heatmapRadiusInput.value = String(Math.round(heatmapSettings.radius));
      heatmapOpacityInput.value = String(Number(heatmapSettings.opacity.toFixed(2)));
      heatmapWindowSecInput.value = String(Math.round(heatmapSettings.durationSec));
      if (data) render(currentTick);
    };
    heatmapIntervalSecInput.addEventListener("change", applyHeatmapNumberInput);
    heatmapRadiusInput.addEventListener("change", applyHeatmapNumberInput);
    heatmapOpacityInput.addEventListener("change", applyHeatmapNumberInput);
    heatmapWindowSecInput.addEventListener("change", applyHeatmapNumberInput);

    canvas.addEventListener("mousedown", (e) => {
      mapView.dragging = true;
      mapView.lastX = e.clientX;
      mapView.lastY = e.clientY;
      canvas.style.cursor = "grabbing";
    });
    window.addEventListener("mouseup", () => {
      mapView.dragging = false;
      canvas.style.cursor = "grab";
    });
    canvas.addEventListener("mouseleave", () => {
      mapView.dragging = false;
      canvas.style.cursor = "grab";
    });
    canvas.addEventListener("mousemove", (e) => {
      if (!mapView.dragging) return;
      const dx = e.clientX - mapView.lastX;
      const dy = e.clientY - mapView.lastY;
      mapView.panX += dx;
      mapView.panY += dy;
      mapView.lastX = e.clientX;
      mapView.lastY = e.clientY;
      if (data) render(currentTick);
    });
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const sx = e.clientX - rect.left;
      const sy = e.clientY - rect.top;
      const pre = screenToPreView(sx, sy);
      const delta = e.deltaY < 0 ? 1.1 : 0.9;
      mapView.zoom = Math.max(mapView.minZoom, Math.min(mapView.maxZoom, mapView.zoom * delta));
      const centerX = canvas.width / 2;
      const centerY = canvas.height / 2;
      mapView.panX = sx - (pre.x - centerX) * mapView.zoom - centerX;
      mapView.panY = sy - (pre.y - centerY) * mapView.zoom - centerY;
      if (data) render(currentTick);
    }, { passive: false });
    canvas.style.cursor = "grab";


    clearCacheBtn.addEventListener("click", async () => {
      if (!data) return;
      const ok = confirm("确定要删除当前录像的缓存文件吗？该操作不可撤销。");
      if (!ok) return;
      const ok2 = confirm("请再次确认：删除后下次将重新解析录像，可能较慢。是否继续？");
      if (!ok2) return;
      try {
        const res = await fetch("/clear_cache", { method: "POST" });
        const obj = await res.json();
        if (obj && obj.deleted) {
          data.cache_hit = false;
          alert(`缓存已删除：${obj.cache_path}`);
        } else {
          alert(`未删除缓存（可能不存在）：${obj && obj.cache_path ? obj.cache_path : "unknown"}`);
        }
      } catch (err) {
        alert(`清理缓存失败：${String(err)}`);
      }
    });


    (async () => {
      // debug+DSR-MAPDBG-01: 记录数据请求与首帧渲染耗时。
      debugLog("data-fetch-start");
      const fetchStartMs = performance.now();
      const res = await fetch("/data");
      debugLog("data-fetch-response", {
        status: res.status,
        elapsedMs: Number((performance.now() - fetchStartMs).toFixed(1)),
      });
      data = await res.json();
      debugLog("data-json-parsed", {
        matchId: data.match_id,
        gameEndTick: data.game_end_tick,
        players: (data.player_timelines || []).length,
      });
      titleLine.textContent = "Dota2 回放可视化";
      slider.min = "0";
      slider.max = String(data.game_end_tick);
      slider.value = "0";
      fpsInput.value = String(data.playback_fps || 30);
      updateSpeedUI();
      initTreeBlockers();
      updateVisionTeamOptions();
      ensureHeroSelectionInitialized();
      rebuildTrailHeroFilterUI();
      applyTrailNumberInput();
      applyHeatmapNumberInput();
      updateTrailToggleText();
      updateHeatmapToggleText();
      updateVisionToggleText();
      renderFromFloat(0);
      debugLog("bootstrap-render-called");
    })();

    window.addEventListener("resize", () => {
      if (!data) return;
      render(currentTick);
    });
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

    html_bytes = HTML_TEMPLATE.encode("utf-8")
    replay_dir = Path("replay_samples").resolve()
    nonlocal_dem_path_holder = {"path": dem_path}
    # debug+DSR-MAPDBG-01: 服务端静态底图读取与请求日志，定位是否卡在图片传输。
    map_bg_path = Path(__file__).resolve().parent / "assets" / "maps" / "map_full.png"
    map_bg_bytes = map_bg_path.read_bytes() if map_bg_path.exists() else None
    print(
        f"[debug+DSR-MAPDBG-01] map-bg-init path={map_bg_path} "
        f"exists={map_bg_path.exists()} size={0 if map_bg_bytes is None else len(map_bg_bytes)}"
    )

    # 将 DEM 解析限制在单后台线程，避免多个 HTTP 请求同时跑 build_gui_payload 争抢 GIL，
    # 造成后入队的解析任务拖慢或明显“卡住”当前正在执行的解析。
    parse_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="replay-parse")

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
                payload_bytes = current_payload_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload_bytes)))
                self.end_headers()
                self.wfile.write(payload_bytes)
                return
            if self.path == "/replays":
                body = json.dumps({"replays": list_replay_records(replay_dir)}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/assets/maps/map_full.png":
                if map_bg_bytes is None:
                    print("[debug+DSR-MAPDBG-01] map-bg-request missing")
                    self.send_response(404)
                    self.end_headers()
                    return
                print(f"[debug+DSR-MAPDBG-01] map-bg-request hit bytes={len(map_bg_bytes)}")
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(map_bg_bytes)))
                self.end_headers()
                self.wfile.write(map_bg_bytes)
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
                        parse_executor.submit(
                            build_gui_payload,
                            dem_ready,
                            int(payload.get("playback_fps", 30)),
                        ).result()
                        _json_response(200, {"ok": True})
                    except Exception as exc:
                        _json_response(400, {"ok": False, "error": str(exc)})
                    return

                if self.path == "/load_replay":
                    try:
                        new_payload, new_dem_path = parse_executor.submit(
                            build_gui_payload,
                            dem_ready,
                            int(payload.get("playback_fps", 30)),
                        ).result()
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
        parse_executor.shutdown(wait=True)
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
