#!/usr/bin/env python3
"""
增强版 Dota 2 回放 GUI（浏览器版）

能力：
1) 从 tick=0 开始播放（而不是从 game_start_tick）
2) 左侧看板（下拉切换：资产、K/D/A、正补/反补、等级；降序）
3) 右侧英雄状态（血量/蓝量、复活倒计时）
4) 英雄死亡时不在地图上绘制图标
5) 播放刷新率（FPS）默认 30，可调
6) Web 界面「录像管理」：下载任务与本机录像合并为一张表、单线程解析队列、备注列与解析进度
"""

from __future__ import annotations

import argparse
import bz2
import json
import os
import sys
import threading
from collections import deque
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import gem
from gem.extractors.players import PlayerExtractor
from gem.parser import ReplayParser
from replay_cache import cache_path_for_dem, delete_replay_cache, load_replay_cache, save_replay_cache
from replay_asset_registry import purge_local_replay_dem_file
from replay_download_io import (
    extract_replays_bz2_archives,
    is_replay_library_path,
    iter_default_replay_candidates,
    list_stored_dem_files,
    migrate_legacy_replay_samples_to_replays,
    replay_storage_root,
)
from replay_download_manager import DownloadTaskManager
from replay_world_entities import WorldEntityCollector

USER_REMARKS_JSON = Path(__file__).resolve().parent / ".dsr_user_remarks.json"


def norm_dem_storage_key(path_obj: Path) -> str:
    """与 Web 端 parse_progress.dem_path 一致的路径键（小写、/、尽量 resolve）。"""
    try:
        return str(path_obj.resolve()).replace("\\", "/").lower()
    except OSError:
        return str(path_obj).replace("\\", "/").lower()


def _load_user_remarks() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {"by_path": {}, "by_task": {}}
    try:
        if USER_REMARKS_JSON.is_file():
            raw = json.loads(USER_REMARKS_JSON.read_text(encoding="utf-8"))
            bp, bt = raw.get("by_path") or {}, raw.get("by_task") or {}
            if isinstance(bp, dict):
                out["by_path"] = {str(k): str(v) for k, v in bp.items() if isinstance(v, str)}
            if isinstance(bt, dict):
                out["by_task"] = {str(k): str(v) for k, v in bt.items() if isinstance(v, str)}
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return out


def _save_user_remarks(data: dict[str, dict[str, str]]) -> None:
    try:
        USER_REMARKS_JSON.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


class ParseCancelledError(Exception):
    """用户取消解析或移出队列时中断 build_gui_payload 内的 parser.parse。"""


def _boot_ms(start: float) -> float:
    """自 start（perf_counter）起的毫秒数，用于启动阶段日志。"""
    return (time.perf_counter() - start) * 1000.0


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
      /* 后出现的 #mapCanvas 会叠在上方；提高控件层 z-index，避免画布抢走按钮的点击命中 */
      isolation: isolate;
    }
    .center > .meta,
    .center > .controls,
    .center > .legend {
      position: relative;
      z-index: 2;
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
    #visionTeamSelect {
      width: 110px;
      padding: 6px 8px;
    }
    .feature-toggle-wrap {
      display: inline-flex;
      align-items: stretch;
      border-radius: 6px;
      overflow: hidden;
      border: 1px solid #2f3946;
      box-sizing: border-box;
      background: #3a4d63;
    }
    .feature-toggle-wrap.on {
      background: #2d6cdf;
    }
    .feature-toggle-main,
    .feature-toggle-settings {
      background: transparent !important;
      color: #fff;
      border: none;
      border-radius: 0;
      cursor: pointer;
    }
    .feature-toggle-main {
      padding: 7px 12px;
      min-width: 56px;
    }
    .feature-toggle-settings {
      width: 36px;
      flex: 0 0 36px;
      border-left: 1px solid rgba(0, 0, 0, 0.2);
      padding: 0;
      font-size: 17px;
      line-height: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      color: rgba(255, 255, 255, 0.95);
    }
    .feature-toggle-wrap:not(.on) .feature-toggle-main:hover,
    .feature-toggle-wrap:not(.on) .feature-toggle-settings:hover {
      background: rgba(255, 255, 255, 0.1) !important;
    }
    .feature-toggle-wrap.on .feature-toggle-main:hover,
    .feature-toggle-wrap.on .feature-toggle-settings:hover {
      background: rgba(255, 255, 255, 0.12) !important;
    }
    button.feature-toggle-vision {
      background: #3a4d63 !important;
      border-radius: 5px;
      min-width: 56px;
      padding: 7px 12px;
    }
    button.feature-toggle-vision.on {
      background: #2d6cdf !important;
    }
    button.feature-toggle-vision:not(.on):hover {
      background: #445a75 !important;
    }
    button.feature-toggle-vision.on:hover {
      background: #3a77e7 !important;
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
      position: relative;
      z-index: 0;
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
      pointer-events: none;
    }
    .settings-modal.open {
      display: flex;
      pointer-events: auto;
    }
    .settings-modal.sub-modal { z-index: 10000; }
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
    .download-manager-body .replay-manager-scroll {
      min-height: 200px;
      max-height: min(58vh, 680px);
    }
    .dl-table .col-check {
      width: 34px;
      text-align: center;
      vertical-align: middle;
    }
    .dl-table .col-check input {
      cursor: pointer;
    }
    .dl-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      table-layout: fixed;
    }
    .dl-table th,
    .dl-table td {
      border: 1px solid #2f3946;
      padding: 5px 8px;
      text-align: left;
      vertical-align: middle;
      word-wrap: break-word;
    }
    .dl-table th {
      background: #1a2330;
      color: #cfe4ff;
      font-weight: 600;
      font-size: 11px;
    }
    .dl-table tbody tr:nth-child(even) { background: #111820; }
    .dl-table tbody tr:hover { background: #182230; }
    .dl-table .dl-remark-cell {
      font-size: 11px;
      color: #b8c4d4;
      max-width: 160px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .dl-table .dl-actions-cell { white-space: nowrap; }
    .dl-table .dl-actions-cell button { padding: 3px 8px; font-size: 11px; margin-right: 4px; }
    .dl-table .dl-actions-cell button.btn-replay-playing:disabled {
      background: #4a5563;
      color: #aeb8c4;
      cursor: not-allowed;
      opacity: 0.92;
    }
    .dl-pager {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px 12px;
      margin-top: 2px;
      font-size: 12px;
      color: #9ba7b6;
    }
    .dl-pager button { padding: 4px 10px; font-size: 11px; background: #3a4d63; }
    .dl-pager button:disabled { opacity: 0.45; cursor: not-allowed; }
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
          <button id="openDownloadManagerBtn" style="background:#1a7f37;">录像管理</button>
          <div class="feature-toggle-wrap">
            <button type="button" id="toggleTrailBtn" class="feature-toggle-main" title="开关英雄轨迹">轨迹</button>
            <button type="button" id="openTrailSettingsBtn" class="feature-toggle-settings" title="轨迹设置" aria-label="轨迹设置">⚙</button>
          </div>
          <div class="feature-toggle-wrap">
            <button type="button" id="toggleHeatmapBtn" class="feature-toggle-main" title="开关热力图">热力图</button>
            <button type="button" id="openHeatmapSettingsBtn" class="feature-toggle-settings" title="热力图设置" aria-label="热力图设置">⚙</button>
          </div>
          <button type="button" id="toggleVisionBtn" class="feature-toggle-vision" title="开关战争迷雾视野">视野</button>
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
          <button id="speedNormalBtn" type="button" style="background:#2d6cdf;" title="1 倍速">1x</button>
          <button id="speedDoubleBtn" style="background:#3a4d63;">2x</button>
          <div class="slider-wrap">
            <input id="slider" type="range" min="0" max="1" step="1" value="0" />
            <label for="fpsInput" class="small-muted">刷新率(FPS)</label>
            <input id="fpsInput" type="number" min="1" max="240" step="1" value="30" style="width: 76px;" />
          </div>
        </div>
      </div>
      <canvas id="mapCanvas" width="1200" height="780"></canvas>
      <div class="legend">英雄：绿/红圆点（天辉/夜魇，死亡不显示） | 建筑：基/塔/营/建 | 单位：近/远/车/野 | 资源点：莲/肉/折 | 守卫：天辉绿/夜魇红，假眼圆、真眼菱形</div>
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
  <div id="downloadManagerModal" class="settings-modal">
    <div class="settings-body download-manager-body" style="width: min(920px, 96vw); max-height: 88vh;">
      <div class="settings-title">
        <span>录像管理</span>
        <button type="button" id="downloadManagerCloseBtn" class="btn-secondary">关闭</button>
      </div>
      <div class="control-row" style="margin-bottom: 10px;">
        <label for="maxConcurrentSelect" class="small-muted">最大同时下载</label>
        <select id="maxConcurrentSelect" style="width: 64px; padding: 5px 8px;">
          <option value="1">1</option>
          <option value="2">2</option>
          <option value="3" selected>3</option>
          <option value="4">4</option>
          <option value="5">5</option>
        </select>
        <button type="button" id="openNewDownloadModalBtn" style="background:#2d6cdf;">新建下载</button>
        <label for="downloadSearchInput" class="small-muted" style="margin-left:8px;">检索</label>
        <input id="downloadSearchInput" type="search" placeholder="录像id、文件名或备注" style="flex:1;min-width:140px;" />
      </div>
      <div id="downloadStorageHint" class="small-muted" style="margin-bottom:4px;line-height:1.35;"></div>
      <div class="control-row" style="margin-bottom:8px;flex-wrap:wrap;align-items:center;">
        <button type="button" id="batchProcessReplaysBtn" class="btn-secondary">批量处理</button>
        <button type="button" id="batchClearCacheReplaysBtn" class="btn-secondary">批量清除缓存</button>
        <span class="small-muted">勾选左侧复选框；「批量处理」仅对选中项中尚未解析的录像入队。</span>
      </div>
      <div id="replayManagerList" class="scroll replay-manager-scroll" style="margin-bottom: 2px;"></div>
      <div id="replayManagerPager" class="dl-pager"></div>
    </div>
  </div>
  <div id="newDownloadModal" class="settings-modal sub-modal">
    <div class="settings-body" style="width: min(440px, 92vw);">
      <div class="settings-title">
        <span>新建下载任务</span>
        <button type="button" id="newDownloadModalCloseBtn" class="btn-secondary">关闭</button>
      </div>
      <div class="settings-grid" style="margin-bottom: 12px; grid-template-columns: minmax(0, 1fr);">
        <label for="modalNewMatchIdInput">录像id</label>
        <input id="modalNewMatchIdInput" type="text" inputmode="numeric" placeholder="例如 8781301871" />
      </div>
      <label class="small-muted" style="margin:0 0 12px 0;display:flex;align-items:center;gap:8px;cursor:pointer;">
        <input type="checkbox" id="autoParseAfterDownloadCheckbox" />
        下载后自动解析
      </label>
      <div class="settings-actions">
        <button type="button" id="cancelNewDownloadBtn" class="btn-secondary">取消</button>
        <button type="button" id="confirmNewDownloadBtn">开始下载</button>
      </div>
    </div>
  </div>
  <div id="clearReplayCacheModal" class="settings-modal sub-modal">
    <div class="settings-body" style="width: min(440px, 92vw);">
      <div class="settings-title">
        <span>清除解析缓存</span>
        <button type="button" id="clearReplayCacheModalCloseBtn" class="btn-secondary">关闭</button>
      </div>
      <p class="small-muted" style="margin:0 0 12px 0;line-height:1.45;">将删除选中项的本地解析缓存（.replay_cache）。默认保留磁盘上的 .dem 录像文件。</p>
      <label class="settings-hero-item" style="margin-bottom:14px;">
        <input type="checkbox" id="clearReplayCacheAlsoDeleteDemCheckbox" />
        <span>同时删除录像文件</span>
      </label>
      <div class="settings-actions">
        <button type="button" id="cancelClearReplayCacheBtn" class="btn-secondary">取消</button>
        <button type="button" id="confirmClearReplayCacheBtn" style="background:#8b1e2d;">确定</button>
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
      if (category === "ward") return "";
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
      if (category === "ward") {
        const tn = Number(timeline.team);
        if (tn === 2) return { fill: "#66bb6a", stroke: "#1b5e20" };
        if (tn === 3) return { fill: "#ef5350", stroke: "#b71c1c" };
        return { fill: "#78909c", stroke: "#37474f" };
      }
      return { fill: "#455a64", stroke: "#eceff1" };
    };

    const drawEntityGlyph = (ctx2, cx, cy, timeline) => {
      const category = timeline.category || "other";
      const subtype = timeline.subtype || "other";
      const colors = entityColors(timeline);
      const glyph = entityGlyph(timeline);
      const radius =
        category === "roshan" || category === "tormentor" ? 9 : category === "ward" ? 5.5 : 7;

      ctx2.strokeStyle = colors.stroke;
      ctx2.fillStyle = colors.fill;
      ctx2.lineWidth = 1.2;
      ctx2.beginPath();
      if (category === "ward") {
        if (subtype === "sentry") {
          const rr = radius * 1.15;
          ctx2.moveTo(cx, cy - rr);
          ctx2.lineTo(cx + rr, cy);
          ctx2.lineTo(cx, cy + rr);
          ctx2.lineTo(cx - rr, cy);
          ctx2.closePath();
        } else {
          ctx2.arc(cx, cy, radius, 0, Math.PI * 2);
        }
        ctx2.fill();
        ctx2.stroke();
      } else if (category === "building" && subtype === "tower") {
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
      if (category !== "ward") {
        ctx2.fill();
        ctx2.stroke();
      }

      if (glyph) {
        ctx2.fillStyle = "#ffffff";
        ctx2.font = "10px Arial";
        ctx2.textAlign = "center";
        ctx2.textBaseline = "middle";
        ctx2.fillText(glyph, cx, cy);
      }
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
    const speedNormalBtn = document.getElementById("speedNormalBtn");
    const speedDoubleBtn = document.getElementById("speedDoubleBtn");
    const slider = document.getElementById("slider");
    const openDownloadManagerBtn = document.getElementById("openDownloadManagerBtn");
    const downloadManagerModal = document.getElementById("downloadManagerModal");
    const downloadManagerCloseBtn = document.getElementById("downloadManagerCloseBtn");
    const maxConcurrentSelect = document.getElementById("maxConcurrentSelect");
    const autoParseAfterDownloadCheckbox = document.getElementById("autoParseAfterDownloadCheckbox");
    const openNewDownloadModalBtn = document.getElementById("openNewDownloadModalBtn");
    const newDownloadModal = document.getElementById("newDownloadModal");
    const newDownloadModalCloseBtn = document.getElementById("newDownloadModalCloseBtn");
    const modalNewMatchIdInput = document.getElementById("modalNewMatchIdInput");
    const confirmNewDownloadBtn = document.getElementById("confirmNewDownloadBtn");
    const cancelNewDownloadBtn = document.getElementById("cancelNewDownloadBtn");
    const downloadSearchInput = document.getElementById("downloadSearchInput");
    const downloadStorageHint = document.getElementById("downloadStorageHint");
    const replayManagerList = document.getElementById("replayManagerList");
    const replayManagerPager = document.getElementById("replayManagerPager");
    const clearReplayCacheModal = document.getElementById("clearReplayCacheModal");
    const clearReplayCacheModalCloseBtn = document.getElementById("clearReplayCacheModalCloseBtn");
    const clearReplayCacheAlsoDeleteDemCheckbox = document.getElementById("clearReplayCacheAlsoDeleteDemCheckbox");
    const cancelClearReplayCacheBtn = document.getElementById("cancelClearReplayCacheBtn");
    const confirmClearReplayCacheBtn = document.getElementById("confirmClearReplayCacheBtn");
    const batchProcessReplaysBtn = document.getElementById("batchProcessReplaysBtn");
    const batchClearCacheReplaysBtn = document.getElementById("batchClearCacheReplaysBtn");
    if (replayManagerList) {
      replayManagerList.addEventListener("change", (e) => {
        const t = e.target;
        if (!t || t.id !== "replayCheckAllPage") return;
        const on = t.checked;
        replayManagerList.querySelectorAll(".replay-row-check:not(:disabled)").forEach((cb) => {
          cb.checked = on;
        });
      });
    }
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
    /** 仅承载迷雾 alpha，在主画布上用 destination-out 会连地图一起抠成透明洞；离屏只画雾再叠上即可露出地图。 */
    let fogScratchCanvas = null;
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
    const clampMapViewPan = () => {
      const W = canvas.width;
      const H = canvas.height;
      if (W <= 0 || H <= 0) return;
      const cx = W / 2;
      const cy = H / 2;
      const z = mapView.zoom;
      const L = mapFramePad;
      const T = mapFramePad;
      const R = W - mapFramePad;
      const B = H - mapFramePad;
      const tL = (L - cx) * z + cx;
      const tR = (R - cx) * z + cx;
      const tT = (T - cy) * z + cy;
      const tB = (B - cy) * z + cy;
      const minSx = Math.min(tL, tR);
      const maxSx = Math.max(tL, tR);
      const minSy = Math.min(tT, tB);
      const maxSy = Math.max(tT, tB);
      const minPanX = W - maxSx;
      const maxPanX = -minSx;
      if (minPanX <= maxPanX) {
        mapView.panX = Math.min(maxPanX, Math.max(minPanX, mapView.panX));
      } else {
        mapView.panX = (W - minSx - maxSx) / 2;
      }
      const minPanY = H - maxSy;
      const maxPanY = -minSy;
      if (minPanY <= maxPanY) {
        mapView.panY = Math.min(maxPanY, Math.max(minPanY, mapView.panY));
      } else {
        mapView.panY = (H - minSy - maxSy) / 2;
      }
    };
    const visionSettings = {
      enabled: false,
      mode: "both",
      heroVisionRadius: 1600,
      treeRadius: 70,
      treeBlockers: [],
      team1: 2,
      team2: 3,
      /** 战争迷雾：视野外暗层不透明度（0~1） */
      fogOpacity: 0.42,
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

    const heroVisionEllipseRadiiPx = (wx, wy) => {
      const [cx, cy] = mapToCanvas(wx, wy, data.map_bounds, canvas);
      const [cxRx, cyRx] = mapToCanvas(wx + visionSettings.heroVisionRadius, wy, data.map_bounds, canvas);
      const [cxRy, cyRy] = mapToCanvas(wx, wy + visionSettings.heroVisionRadius, data.map_bounds, canvas);
      const rx = Math.hypot(cxRx - cx, cyRx - cy);
      const ry = Math.hypot(cxRy - cx, cyRy - cy);
      return { cx, cy, rx, ry };
    };

    const renderVisionFogOfWar = (tick) => {
      if (!data || !visionSettings.enabled) return;
      const mx0 = mapFramePad;
      const my0 = mapFramePad;
      const mw = Math.max(0, canvas.width - 2 * mapFramePad);
      const mh = Math.max(0, canvas.height - 2 * mapFramePad);
      if (mw <= 0 || mh <= 0) return;

      if (!fogScratchCanvas || fogScratchCanvas.width !== canvas.width || fogScratchCanvas.height !== canvas.height) {
        fogScratchCanvas = document.createElement("canvas");
        fogScratchCanvas.width = canvas.width;
        fogScratchCanvas.height = canvas.height;
      }
      const fctx = fogScratchCanvas.getContext("2d");
      fctx.setTransform(1, 0, 0, 1, 0, 0);
      fctx.clearRect(0, 0, canvas.width, canvas.height);
      fctx.globalAlpha = 1;
      fctx.globalCompositeOperation = "source-over";
      fctx.save();
      const centerX = canvas.width / 2;
      const centerY = canvas.height / 2;
      fctx.translate(centerX + mapView.panX, centerY + mapView.panY);
      fctx.scale(mapView.zoom, mapView.zoom);
      fctx.translate(-centerX, -centerY);

      fctx.beginPath();
      fctx.rect(mx0, my0, mw, mh);
      fctx.clip();

      const fogA = Math.max(0, Math.min(1, visionSettings.fogOpacity));
      fctx.fillStyle = `rgba(0, 0, 0, ${fogA})`;
      fctx.fillRect(mx0, my0, mw, mh);

      fctx.globalCompositeOperation = "destination-out";
      fctx.fillStyle = "#ffffff";
      const sources = getVisionSourceTimelines();
      for (const source of sources) {
        const st = stateAtTick(source, tick);
        if (!st || st.x === null || st.y === null || st.hp <= 0) continue;
        if (deathInfoAtTick(source, tick).is_dead) continue;
        const { cx, cy, rx, ry } = heroVisionEllipseRadiiPx(st.x, st.y);
        fctx.beginPath();
        fctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
        fctx.fill();
      }
      fctx.restore();

      ctx.save();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.globalCompositeOperation = "source-over";
      ctx.globalAlpha = 1;
      ctx.drawImage(fogScratchCanvas, 0, 0);
      ctx.restore();
    };

    const renderMap = (tick) => {
      resizeCanvasToMapAspect();
      clampMapViewPan();
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
      renderVisionFogOfWar(tick);
      ctx.restore();
    };

    const ensureHeroSelectionInitialized = () => {
      if (!data || heroSelectionInitialized) return;
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
      const w = toggleTrailBtn && toggleTrailBtn.closest(".feature-toggle-wrap");
      if (w) w.classList.toggle("on", heroTrailSettings.enabled);
    };
    const updateHeatmapToggleText = () => {
      const w = toggleHeatmapBtn && toggleHeatmapBtn.closest(".feature-toggle-wrap");
      if (w) w.classList.toggle("on", heatmapSettings.enabled);
    };
    const updateVisionToggleText = () => {
      toggleVisionBtn.classList.toggle("on", visionSettings.enabled);
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
      if (!data || data.session_ready === false) return;
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
      if (!data || data.session_ready === false) return;
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

    const applyLoadedPayload = () => {
      if (!data) return;
      if (data.session_ready === false) {
        titleLine.textContent = "Dota2 回放可视化 · 请在录像管理中选择录像或处理缓存";
        slider.min = "0";
        slider.max = "0";
        slider.value = "0";
        stopPlayback();
        if (downloadManagerModal.classList.contains("open")) renderDownloadTaskList();
        return;
      }
      titleLine.textContent = `Dota2 回放可视化 · match ${data.match_id}`;
      slider.min = "0";
      slider.max = String(data.game_end_tick);
      slider.value = "0";
      currentTick = 0;
      currentTickFloat = 0;
      fpsInput.value = String(data.playback_fps || 30);
      updateSpeedUI();
      initTreeBlockers();
      updateVisionTeamOptions();
      heroSelectionInitialized = false;
      ensureHeroSelectionInitialized();
      rebuildTrailHeroFilterUI();
      applyTrailNumberInput();
      applyHeatmapNumberInput();
      updateTrailToggleText();
      updateHeatmapToggleText();
      updateVisionToggleText();
      mapView.zoom = 1.0;
      mapView.panX = 0.0;
      mapView.panY = 0.0;
      clampMapViewPan();
      renderFromFloat(0);
      if (downloadManagerModal.classList.contains("open")) renderDownloadTaskList();
    };

    const reloadDataFromServer = async () => {
      stopPlayback();
      const res = await fetch("/data", { cache: "no-store" });
      if (!res.ok) throw new Error(`拉取数据失败 HTTP ${res.status}`);
      data = await res.json();
      if (data.parse_progress) {
        lastDownloadPayload.parse_progress = data.parse_progress;
        if (!Array.isArray(lastDownloadPayload.parse_progress.queued_paths)) {
          lastDownloadPayload.parse_progress.queued_paths = [];
        }
      }
      applyLoadedPayload();
    };

    const updateSpeedUI = () => {
      speedHalfBtn.style.background = playbackSpeed === 0.5 ? "#2d6cdf" : "#3a4d63";
      if (speedNormalBtn) speedNormalBtn.style.background = playbackSpeed === 1.0 ? "#2d6cdf" : "#3a4d63";
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
      if (!data || data.session_ready === false) return;
      const deltaTick = deltaSec * data.tick_rate;
      renderFromFloat(currentTickFloat + deltaTick);
      if (playing) {
        playbackAnchorRealMs = performance.now();
        playbackAnchorTick = currentTickFloat;
      }
    };

    const startPlayback = () => {
      if (!data || data.session_ready === false) return;
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

    playBtn.addEventListener("click", () => {
      if (!data || data.session_ready === false) return;
      if (playing) stopPlayback();
      else startPlayback();
    });
    seekBack10Btn.addEventListener("click", () => seekBySeconds(-10));
    seekForward10Btn.addEventListener("click", () => seekBySeconds(10));
    speedHalfBtn.addEventListener("click", () => setPlaybackSpeed(0.5));
    if (speedNormalBtn) speedNormalBtn.addEventListener("click", () => setPlaybackSpeed(1.0));
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
      if (!data || data.session_ready === false) return;
      renderFromFloat(Number(e.target.value));
      if (playing) {
        playbackAnchorRealMs = performance.now();
        playbackAnchorTick = currentTickFloat;
      }
    });

    boardMetric.addEventListener("change", () => {
      if (!data || data.session_ready === false) return;
      renderBoard(currentTick);
    });

    fpsInput.addEventListener("change", () => {
      if (!data || data.session_ready === false) return;
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
      if (!data || data.session_ready === false) return;
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
      clampMapViewPan();
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
      clampMapViewPan();
      if (data) render(currentTick);
    }, { passive: false });
    canvas.style.cursor = "grab";

    let downloadPollTimer = null;
    let pendingReplayCacheClearItems = [];
    let lastDownloadPayload = {
      tasks: [],
      max_concurrent: 3,
      auto_parse_after_download: false,
      local_replays: [],
      storage_roots: {},
      parse_progress: { active: false, dem_path: null, pct: 0, queued_paths: [] },
    };
    let replayManagerPage = 1;
    const DL_PAGE_SIZE = 10;

    const dlAttrEsc = (s) =>
      String(s ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll('"', "&quot;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");

    const formatDlEta = (sec) => {
      if (sec === null || sec === undefined || !Number.isFinite(Number(sec))) return "—";
      const n = Number(sec);
      if (n < 0 || n > 86400 * 14) return "—";
      const s = Math.floor(n);
      const m = Math.floor(s / 60);
      const h = Math.floor(m / 60);
      if (h > 0) return `${h}小时${m % 60}分`;
      if (m > 0) return `${m}分${s % 60}秒`;
      return `${s}秒`;
    };

    const formatDlBytes = (n) => {
      const v = Number(n);
      if (!Number.isFinite(v) || v < 0) return "—";
      if (v < 1024) return `${Math.round(v)} B`;
      if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
      if (v < 1024 * 1024 * 1024) return `${(v / (1024 * 1024)).toFixed(1)} MB`;
      return `${(v / (1024 * 1024 * 1024)).toFixed(2)} GB`;
    };

    function truncateToMinute(timeStr) {
      if (!timeStr || typeof timeStr !== "string") return "—";
      const s = timeStr.trim();
      const m = s.match(/^(\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2})/);
      if (m) return m[1];
      return s.length >= 10 ? s.slice(0, 10) : s;
    }

    async function fetchDownloadTasksRaw() {
      const res = await fetch("/api/downloads");
      if (!res.ok) return null;
      const j = await res.json();
      if (!j.parse_progress) j.parse_progress = { active: false, dem_path: null, pct: 0, queued_paths: [] };
      if (!Array.isArray(j.parse_progress.queued_paths)) j.parse_progress.queued_paths = [];
      if (j.auto_parse_after_download === undefined) j.auto_parse_after_download = false;
      return j;
    }

    async function afterLoadStartPlayingFromManager() {
      await reloadDataFromServer();
      closeNewDownloadModal();
      downloadManagerModal.classList.remove("open");
      if (downloadPollTimer) {
        clearInterval(downloadPollTimer);
        downloadPollTimer = null;
      }
      startPlayback();
    }

    function normDemPathKey(p) {
      if (!p) return "";
      return String(p).replaceAll("\\\\", "/").toLowerCase();
    }

    function demKeyForParseProgress(pathStr) {
      return normDemPathKey(pathStr).replace(/\\.bz2$/i, "");
    }

    function currentPlayingDemKey() {
      if (!data || data.session_ready === false) return "";
      const dp = data.dem_path;
      if (dp == null || String(dp).trim() === "") return "";
      return demKeyForParseProgress(String(dp));
    }

    function parseQueuedPaths() {
      const pp = lastDownloadPayload.parse_progress || {};
      return Array.isArray(pp.queued_paths) ? pp.queued_paths : [];
    }

    function isParseActiveForKey(key) {
      if (!key) return false;
      const pp = lastDownloadPayload.parse_progress || {};
      return Boolean(pp.active && pp.dem_path === key);
    }

    function isParseQueuedForKey(key) {
      if (!key) return false;
      return parseQueuedPaths().includes(key);
    }

    function localStatusText(row) {
      const key = row.dem_norm_key || demKeyForParseProgress(String(row.path || ""));
      if (key && key === currentPlayingDemKey()) return "播放中";
      const pp = lastDownloadPayload.parse_progress || {};
      if (isParseActiveForKey(key)) return `处理中（${Math.round(Number(pp.pct) || 0)}%）`;
      if (isParseQueuedForKey(key)) return "队列中";
      if (row.playback_ready) return "可播放";
      return "待处理";
    }

    function isTaskDownloading(t) {
      if (t.state === "running") return true;
      if (t.state === "paused" && t.phase && t.phase !== "queued") return true;
      return false;
    }

    function taskStatusText(t) {
      const pp = lastDownloadPayload.parse_progress || {};
      const pct = Math.min(100, Math.round((t.progress || 0) * 100));
      if (t.state === "completed") {
        const pk = t.dem_norm_key || (t.output_dem_path ? demKeyForParseProgress(String(t.output_dem_path)) : "");
        if (pk && pk === currentPlayingDemKey()) return "播放中";
        if (!t.playback_ready && pk) {
          if (isParseActiveForKey(pk)) return `处理中（${Math.round(Number(pp.pct) || 0)}%）`;
          if (isParseQueuedForKey(pk)) return "队列中";
        }
        return t.playback_ready ? "可播放" : "待处理";
      }
      if (t.state === "error") return "失败";
      if (t.state === "cancelled") return "已取消";
      if (isTaskDownloading(t)) {
        return t.state === "paused" ? `下载中（已暂停 ${pct}%）` : `下载中（${pct}%）`;
      }
      return "待处理";
    }

    function taskRemarkMeta(t) {
      const parts = [];
      if (t.created_at) parts.push(`创建 ${t.created_at}`);
      if (t.download_started_at) parts.push(`开始 ${t.download_started_at}`);
      if (isTaskDownloading(t) || t.state === "waiting_slot") {
        const eta = formatDlEta(t.eta_seconds);
        if (eta !== "—") parts.push(`剩余 ${eta}`);
      }
      if (t.state === "error" && t.error_message) parts.push(String(t.error_message).slice(0, 160));
      return parts.length ? parts.join(" · ") : "";
    }

    function isTaskTerminal(t) {
      return ["completed", "error", "cancelled"].includes(t.state);
    }

    function taskActiveOrder(t) {
      if (t.state === "running") return 0;
      if (t.state === "waiting_slot") return 1;
      if (t.state === "queued") return 2;
      if (t.state === "paused") return 3;
      return 9;
    }

    function filterLocalsByQuery(locals, q) {
      if (!q) return locals;
      return locals.filter(
        (row) =>
          String(row.match_id_hint || "").includes(q) ||
          String(row.name || "").toLowerCase().includes(q) ||
          String(row.path || "").toLowerCase().includes(q) ||
          String(row.user_remark || "").toLowerCase().includes(q)
      );
    }

    function buildMergedReplayRows(allTasks, allLocals) {
      const completedPaths = new Set();
      for (const t of allTasks) {
        if (t.state === "completed" && t.output_dem_path) {
          const k = t.dem_norm_key || demKeyForParseProgress(String(t.output_dem_path));
          if (k) completedPaths.add(k);
        }
      }
      const localsOnly = [];
      for (const row of allLocals) {
        const p = row.dem_norm_key || demKeyForParseProgress(String(row.path || ""));
        if (completedPaths.has(p)) continue;
        localsOnly.push(row);
      }
      const items = [];
      for (const t of allTasks) items.push({ kind: "task", task: t });
      for (const row of localsOnly) items.push({ kind: "local", local: row });
      items.sort((a, b) => {
        const ac = a.kind === "task" && !isTaskTerminal(a.task) ? 0 : 1;
        const bc = b.kind === "task" && !isTaskTerminal(b.task) ? 0 : 1;
        if (ac !== bc) return ac - bc;
        if (ac === 0 && a.kind === "task" && b.kind === "task") {
          const oa = taskActiveOrder(a.task);
          const ob = taskActiveOrder(b.task);
          if (oa !== ob) return oa - ob;
          return String(b.task.created_at || "").localeCompare(String(a.task.created_at || ""));
        }
        const ta =
          a.kind === "task"
            ? String(a.task.download_started_at || a.task.created_at || "")
            : String(a.local.modified || "");
        const tb =
          b.kind === "task"
            ? String(b.task.download_started_at || b.task.created_at || "")
            : String(b.local.modified || "");
        if (ta !== tb) return tb.localeCompare(ta);
        if (a.kind === "task" && b.kind === "task") return String(b.task.match_id).localeCompare(String(a.task.match_id));
        if (a.kind === "local" && b.kind === "local") return String(b.local.name || "").localeCompare(String(a.local.name || ""));
        return a.kind === "task" ? -1 : 1;
      });
      return items;
    }

    function buildClearCacheItemFromReplayButton(btn) {
      const k = btn.getAttribute("data-kind");
      if (k === "task") {
        const tid = btn.getAttribute("data-task-id");
        if (!tid) return null;
        return { kind: "task", task_id: tid };
      }
      if (k === "local") {
        const enc = btn.getAttribute("data-local-path");
        if (!enc) return null;
        try {
          return { kind: "local", path: decodeURIComponent(enc) };
        } catch (e) {
          return null;
        }
      }
      return null;
    }

    function openReplayClearCacheModal(items) {
      const arr = (items || []).filter(Boolean);
      if (!arr.length) return;
      pendingReplayCacheClearItems = arr;
      if (clearReplayCacheAlsoDeleteDemCheckbox) clearReplayCacheAlsoDeleteDemCheckbox.checked = false;
      if (clearReplayCacheModal) clearReplayCacheModal.classList.add("open");
    }

    function closeReplayClearCacheModal() {
      if (clearReplayCacheModal) clearReplayCacheModal.classList.remove("open");
      pendingReplayCacheClearItems = [];
    }

    function collectReplayManagerCheckedItems() {
      if (!replayManagerList) return [];
      const out = [];
      replayManagerList.querySelectorAll(".replay-row-check:checked").forEach((cb) => {
        const k = cb.getAttribute("data-kind");
        if (k === "task") {
          const tid = cb.getAttribute("data-task-id");
          if (tid) out.push({ kind: "task", task_id: tid });
        } else if (k === "local") {
          const enc = cb.getAttribute("data-path-enc");
          if (enc) {
            try {
              out.push({ kind: "local", path: decodeURIComponent(enc) });
            } catch (e) {
              /* ignore */
            }
          }
        }
      });
      return out;
    }

    function renderPagerBar(page, total, pageSize) {
      if (!replayManagerPager) return;
      if (total === 0) {
        replayManagerPager.innerHTML = "";
        return;
      }
      const totalPages = Math.max(1, Math.ceil(total / pageSize));
      if (total <= pageSize) {
        replayManagerPager.innerHTML = `<span class="small-muted">共 ${total} 条</span>`;
        return;
      }
      const from = (page - 1) * pageSize + 1;
      const to = Math.min(total, page * pageSize);
      const d1 = page <= 1 ? 'disabled' : '';
      const d2 = page >= totalPages ? 'disabled' : '';
      replayManagerPager.innerHTML = `<span class="small-muted">共 ${total} 条，${from}–${to}</span><button type="button" class="dl-pager-btn" data-pager-dir="-1" ${d1}>上一页</button><span class="small-muted">第 ${page} / ${totalPages} 页</span><button type="button" class="dl-pager-btn" data-pager-dir="1" ${d2}>下一页</button>`;
      replayManagerPager.querySelectorAll(".dl-pager-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          const d = parseInt(btn.getAttribute("data-pager-dir") || "0", 10);
          const tp = Math.max(1, Math.ceil(total / pageSize));
          replayManagerPage = Math.max(1, Math.min(tp, replayManagerPage + d));
          renderDownloadTaskList();
        });
      });
    }

    function renderDownloadTaskList() {
      const roots = lastDownloadPayload.storage_roots || {};
      if (downloadStorageHint) {
        const rp = roots.replays || "";
        downloadStorageHint.textContent = rp ? `录像目录 replays：${rp}` : "";
      }
      if (!replayManagerList) return;

      const allTasks = lastDownloadPayload.tasks || [];
      const allLocals = lastDownloadPayload.local_replays || [];
      const q = (downloadSearchInput.value || "").trim().toLowerCase();
      const tasksF = q
        ? allTasks.filter(
            (t) =>
              String(t.match_id).includes(q) ||
              String(t.display_stem || "").toLowerCase().includes(q) ||
              String(t.user_remark || "").toLowerCase().includes(q)
          )
        : allTasks;
      const localsF = filterLocalsByQuery(allLocals, q);
      const merged = buildMergedReplayRows(tasksF, localsF);

      syncDownloadSettingsControls();

      if (allTasks.length === 0 && allLocals.length === 0) {
        replayManagerList.innerHTML =
          '<div class="small-muted">暂无录像。点击「新建下载」添加下载任务；本机 replays/ 下的 .dem 也会显示在此。</div>';
        if (replayManagerPager) replayManagerPager.innerHTML = "";
        replayManagerPage = 1;
        return;
      }
      if (merged.length === 0) {
        replayManagerList.innerHTML = '<div class="small-muted">无匹配录像，请调整检索关键字。</div>';
        if (replayManagerPager) replayManagerPager.innerHTML = "";
        return;
      }

      const totalPages = Math.max(1, Math.ceil(merged.length / DL_PAGE_SIZE));
      if (replayManagerPage > totalPages) replayManagerPage = totalPages;
      const t0 = (replayManagerPage - 1) * DL_PAGE_SIZE;
      const slice = merged.slice(t0, t0 + DL_PAGE_SIZE);

      const bodyRows = slice
        .map((item, i) => {
          const gIdx = t0 + i;
          if (item.kind === "task") {
            const t = item.task;
            const metaTip = taskRemarkMeta(t);
            const titleAttr = metaTip ? ` title="${dlAttrEsc(metaTip)}"` : "";
            const vid = String(t.match_id ?? "");
            const chkDis =
              t.state !== "completed" || !t.output_dem_path
                ? ` disabled title=\"仅已完成且有录像文件的任务可参与批量清除缓存\"`
                : "";
            const chk = `<td class="col-check"><input type="checkbox" class="replay-row-check" data-kind="task" data-task-id="${t.id}"${chkDis} /></td>`;
            const sz =
              t.output_size != null && Number.isFinite(Number(t.output_size))
                ? formatDlBytes(t.output_size)
                : "—";
            const timeCol = truncateToMinute(t.download_started_at || t.created_at || "");
            const st = taskStatusText(t);
            const uraw = String(t.user_remark || "");
            const ur = dlAttrEsc(uraw);
            const pk = t.dem_norm_key || (t.output_dem_path ? demKeyForParseProgress(String(t.output_dem_path)) : "");
            const inParse = Boolean(pk && (isParseActiveForKey(pk) || isParseQueuedForKey(pk)));
            const isPlaying = Boolean(pk && pk === currentPlayingDemKey());
            const tail =
              t.state === "completed"
                ? `<button type="button" class="btn-replay-clear-cache" data-kind="task" data-task-id="${t.id}" style="background:#8b1e2d;">清除缓存</button>`
                : `<button type="button" class="btn-dl-delete" data-task-id="${t.id}" style="background:#8b1e2d;">删除任务</button>`;
            let actions = tail;
            if (t.state === "completed") {
              if (isPlaying) {
                actions = `<button type="button" class="btn-dl-play btn-replay-playing" data-task-id="${t.id}" disabled title="当前正在播放">播放</button>${actions}`;
              } else if (t.playback_ready) {
                actions = `<button type="button" class="btn-dl-play" data-task-id="${t.id}" style="background:#1a7f37;">播放</button>${actions}`;
              } else if (inParse) {
                actions = `<button type="button" class="btn-parse-cancel" data-dem-key="${dlAttrEsc(pk)}" data-task-id="${t.id}" style="background:#8a5a2b;">取消</button>${actions}`;
              } else {
                actions = `<button type="button" class="btn-dl-process" data-task-id="${t.id}" style="background:#6b4dbf;">处理</button>${actions}`;
              }
            } else {
              actions = tail;
            }
            return `<tr data-merged-idx="${gIdx}"${titleAttr}>
              ${chk}
              <td>${dlAttrEsc(vid)}</td>
              <td>${sz}</td>
              <td class="small-muted">${dlAttrEsc(timeCol)}</td>
              <td>${dlAttrEsc(st)}</td>
              <td class="dl-actions-cell">${actions}</td>
              <td class="dl-remark-cell"><span title="${ur}">${uraw ? ur : "—"}</span> <button type="button" class="btn-secondary btn-remark-task" data-task-id="${t.id}" style="padding:3px 8px;font-size:11px;">编辑</button></td>
            </tr>`;
          }
          const row = item.local;
          const vid =
            row.match_id_hint != null && row.match_id_hint !== undefined ? String(row.match_id_hint) : "—";
          const pathEnc = encodeURIComponent(String(row.path || ""));
          const chk = `<td class="col-check"><input type="checkbox" class="replay-row-check" data-kind="local" data-path-enc="${pathEnc}" /></td>`;
          const sz = formatDlBytes(row.size);
          const timeCol = truncateToMinute(String(row.modified || ""));
          const st = localStatusText(row);
          const uraw = String(row.user_remark || "");
          const ur = dlAttrEsc(uraw);
          const pk = row.dem_norm_key || demKeyForParseProgress(String(row.path || ""));
          const inParse = Boolean(pk && (isParseActiveForKey(pk) || isParseQueuedForKey(pk)));
          const isPlaying = Boolean(pk && pk === currentPlayingDemKey());
          let act = `<button type="button" class="btn-replay-clear-cache" data-kind="local" data-local-path="${pathEnc}" style="background:#8b1e2d;">清除缓存</button>`;
          if (isPlaying) {
            act = `<button type="button" class="btn-local-play btn-replay-playing" data-local-path="${pathEnc}" disabled title="当前正在播放">播放</button>${act}`;
          } else if (row.playback_ready) {
            act = `<button type="button" class="btn-local-play" data-local-path="${pathEnc}" style="background:#1a7f37;">播放</button>${act}`;
          } else if (inParse) {
            act = `<button type="button" class="btn-parse-cancel" data-dem-key="${dlAttrEsc(pk)}" style="background:#8a5a2b;">取消</button>${act}`;
          } else {
            act = `<button type="button" class="btn-local-process" data-local-path="${pathEnc}" style="background:#6b4dbf;">处理</button>${act}`;
          }
          return `<tr data-merged-idx="${gIdx}">
            ${chk}
            <td>${dlAttrEsc(vid)}</td>
            <td>${sz}</td>
            <td class="small-muted">${dlAttrEsc(timeCol)}</td>
            <td>${st}</td>
            <td class="dl-actions-cell">${act}</td>
            <td class="dl-remark-cell"><span title="${ur}">${uraw ? ur : "—"}</span> <button type="button" class="btn-secondary btn-remark-local" data-local-path="${pathEnc}" style="padding:3px 8px;font-size:11px;">编辑</button></td>
          </tr>`;
        })
        .join("");

      replayManagerList.innerHTML = `<table class="dl-table"><thead><tr><th class="col-check"><input type="checkbox" id="replayCheckAllPage" title="本页全选" /></th><th>录像id</th><th>大小</th><th>时间</th><th>状态</th><th>操作</th><th>备注</th></tr></thead><tbody>${bodyRows}</tbody></table>`;
      renderPagerBar(replayManagerPage, merged.length, DL_PAGE_SIZE);

      const postCancelParse = async (btn) => {
        const key = btn.getAttribute("data-dem-key");
        const tid = btn.getAttribute("data-task-id");
        try {
          const body = {};
          if (key) body.dem_norm_key = key;
          if (tid) body.task_id = tid;
          if (!body.dem_norm_key && !body.task_id) return;
          const res = await fetch("/api/downloads/parse/cancel", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          const o = await res.json().catch(() => ({}));
          if (!res.ok || o.ok === false) throw new Error(o.error || res.statusText);
          const j = await fetchDownloadTasksRaw();
          if (j) lastDownloadPayload = j;
          renderDownloadTaskList();
        } catch (e) {
          alert(`取消失败：${e.message || e}`);
        }
      };
      replayManagerList.querySelectorAll(".btn-parse-cancel").forEach((b) => {
        b.addEventListener("click", () => postCancelParse(b));
      });

      replayManagerList.querySelectorAll(".btn-remark-task").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const id = btn.getAttribute("data-task-id");
          if (!id) return;
          const cur = (lastDownloadPayload.tasks || []).find((x) => x.id === id);
          const v0 = cur && cur.user_remark ? String(cur.user_remark) : "";
          const v = prompt("备注（可留空）", v0);
          if (v === null) return;
          try {
            const res = await fetch(`/api/downloads/${id}`, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ user_remark: v }),
            });
            const o = await res.json();
            if (!res.ok || !o.ok) throw new Error(o.error || res.statusText);
            const j = await fetchDownloadTasksRaw();
            if (j) lastDownloadPayload = j;
            renderDownloadTaskList();
          } catch (e) {
            alert(`保存备注失败：${e.message || e}`);
          }
        });
      });
      replayManagerList.querySelectorAll(".btn-remark-local").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const enc = btn.getAttribute("data-local-path");
          if (!enc) return;
          let pathStr = "";
          try {
            pathStr = decodeURIComponent(enc);
          } catch (e) {
            return;
          }
          const cur = (lastDownloadPayload.local_replays || []).find((x) => String(x.path) === pathStr);
          const v0 = cur && cur.user_remark ? String(cur.user_remark) : "";
          const v = prompt("备注（可留空）", v0);
          if (v === null) return;
          try {
            const res = await fetch("/api/downloads/local/remark", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ path: pathStr, user_remark: v }),
            });
            const o = await res.json();
            if (!res.ok || !o.ok) throw new Error(o.error || res.statusText);
            const j = await fetchDownloadTasksRaw();
            if (j) lastDownloadPayload = j;
            renderDownloadTaskList();
          } catch (e) {
            alert(`保存备注失败：${e.message || e}`);
          }
        });
      });

      replayManagerList.querySelectorAll(".btn-dl-process").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const id = btn.getAttribute("data-task-id");
          if (!id) return;
          btn.disabled = true;
          try {
            const res = await fetch(`/api/downloads/${id}/process`, { method: "POST" });
            const o = await res.json().catch(() => ({}));
            if (!res.ok || o.ok === false) throw new Error(o.error || res.statusText);
            const j = await fetchDownloadTasksRaw();
            if (j) lastDownloadPayload = j;
            renderDownloadTaskList();
          } catch (e) {
            alert(`处理失败：${e.message || e}`);
          } finally {
            btn.disabled = false;
          }
        });
      });
      replayManagerList.querySelectorAll(".btn-dl-play").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const id = btn.getAttribute("data-task-id");
          if (!id) return;
          try {
            const res = await fetch(`/api/downloads/${id}/play`, { method: "POST" });
            const o = await res.json().catch(() => ({}));
            if (!res.ok || o.ok === false) throw new Error(o.error || res.statusText);
            await afterLoadStartPlayingFromManager();
          } catch (e) {
            alert(`播放失败：${e.message || e}`);
          }
        });
      });
      replayManagerList.querySelectorAll(".btn-dl-delete").forEach((b) =>
        b.addEventListener("click", async () => {
          if (!confirm("确定删除该任务、已下载文件及其解析缓存？")) return;
          const id = b.getAttribute("data-task-id");
          try {
            const res = await fetch(`/api/downloads/${id}`, { method: "DELETE" });
            const o = await res.json().catch(() => ({}));
            if (!res.ok || o.ok === false) throw new Error(o.error || res.statusText);
            const j = await fetchDownloadTasksRaw();
            if (j) lastDownloadPayload = j;
            renderDownloadTaskList();
          } catch (e) {
            alert(`删除失败：${e.message || e}`);
          }
        })
      );
      replayManagerList.querySelectorAll(".btn-local-play").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const enc = btn.getAttribute("data-local-path");
          if (!enc) return;
          let pathStr = "";
          try {
            pathStr = decodeURIComponent(enc);
          } catch (e) {
            return;
          }
          try {
            const res = await fetch("/api/downloads/local/play", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ path: pathStr }),
            });
            const o = await res.json().catch(() => ({}));
            if (!res.ok || o.ok === false) throw new Error(o.error || res.statusText);
            await afterLoadStartPlayingFromManager();
          } catch (e) {
            alert(`播放失败：${e.message || e}`);
          }
        });
      });
      replayManagerList.querySelectorAll(".btn-local-process").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const enc = btn.getAttribute("data-local-path");
          if (!enc) return;
          let pathStr = "";
          try {
            pathStr = decodeURIComponent(enc);
          } catch (e) {
            return;
          }
          btn.disabled = true;
          try {
            const res = await fetch("/api/downloads/local/process", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ path: pathStr }),
            });
            const o = await res.json().catch(() => ({}));
            if (!res.ok || o.ok === false) throw new Error(o.error || res.statusText);
            const j = await fetchDownloadTasksRaw();
            if (j) lastDownloadPayload = j;
            renderDownloadTaskList();
          } catch (e) {
            alert(`处理失败：${e.message || e}`);
          } finally {
            btn.disabled = false;
          }
        });
      });
      replayManagerList.querySelectorAll(".btn-replay-clear-cache").forEach((btn) => {
        btn.addEventListener("click", () => {
          const one = buildClearCacheItemFromReplayButton(btn);
          openReplayClearCacheModal(one ? [one] : []);
        });
      });
    }

    async function pollDownloadTasksOnce() {
      const j = await fetchDownloadTasksRaw();
      if (j) {
        lastDownloadPayload = j;
        if (downloadManagerModal.classList.contains("open")) renderDownloadTaskList();
      }
    }

    openDownloadManagerBtn.addEventListener("click", async () => {
      replayManagerPage = 1;
      downloadManagerModal.classList.add("open");
      await pollDownloadTasksOnce();
      renderDownloadTaskList();
      if (downloadPollTimer) clearInterval(downloadPollTimer);
      downloadPollTimer = setInterval(pollDownloadTasksOnce, 500);
    });
    const closeNewDownloadModal = () => {
      newDownloadModal.classList.remove("open");
      modalNewMatchIdInput.value = "";
    };

    downloadManagerCloseBtn.addEventListener("click", () => {
      closeNewDownloadModal();
      closeReplayClearCacheModal();
      downloadManagerModal.classList.remove("open");
      if (downloadPollTimer) {
        clearInterval(downloadPollTimer);
        downloadPollTimer = null;
      }
    });
    downloadManagerModal.addEventListener("click", (e) => {
      if (e.target !== downloadManagerModal) return;
      if (clearReplayCacheModal && clearReplayCacheModal.classList.contains("open")) {
        closeReplayClearCacheModal();
        return;
      }
      if (newDownloadModal.classList.contains("open")) {
        closeNewDownloadModal();
        return;
      }
      downloadManagerCloseBtn.click();
    });
    function hasExistingReplayForMatchId(mid) {
      const n = Number(mid);
      if (!Number.isFinite(n) || n <= 0) return false;
      const locals = lastDownloadPayload.local_replays || [];
      for (const row of locals) {
        const hint = row.match_id_hint;
        if (hint != null && Number(hint) === n) return true;
        const nm = String(row.name || "");
        if (new RegExp(`_${n}\\.(?:dem|dem\\.bz2)$`, "i").test(nm)) return true;
        if (new RegExp(`^${n}\\.dem(?:\\.bz2)?$`, "i").test(nm)) return true;
      }
      const tasks = lastDownloadPayload.tasks || [];
      for (const t of tasks) {
        if (Number(t.match_id) !== n) continue;
        if (t.state === "cancelled") continue;
        return true;
      }
      return false;
    }

    function syncDownloadSettingsControls() {
      const mcVal = String(Math.max(1, Math.min(5, Number(lastDownloadPayload.max_concurrent) || 3)));
      if (maxConcurrentSelect.value !== mcVal) maxConcurrentSelect.value = mcVal;
      const autoParseWant = Boolean(lastDownloadPayload.auto_parse_after_download);
      if (autoParseAfterDownloadCheckbox && autoParseAfterDownloadCheckbox.checked !== autoParseWant) {
        autoParseAfterDownloadCheckbox.checked = autoParseWant;
      }
    }
    function applyDownloadSettingsPayload(o) {
      if (o.max_concurrent !== undefined) lastDownloadPayload.max_concurrent = o.max_concurrent;
      if (o.auto_parse_after_download !== undefined) {
        lastDownloadPayload.auto_parse_after_download = Boolean(o.auto_parse_after_download);
      }
      syncDownloadSettingsControls();
    }
    async function refreshDownloadSettingsFromServer() {
      const j = await fetchDownloadTasksRaw();
      if (j) {
        lastDownloadPayload = j;
        syncDownloadSettingsControls();
      }
    }
    async function postDownloadSettings(partial) {
      const res = await fetch("/api/downloads/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(partial),
      });
      const o = await res.json().catch(() => ({}));
      if (!res.ok || !o.ok) throw new Error(o.error || res.statusText);
      applyDownloadSettingsPayload(o);
      return o;
    }
    downloadSearchInput.addEventListener("input", () => {
      replayManagerPage = 1;
      renderDownloadTaskList();
    });
    maxConcurrentSelect.addEventListener("change", async () => {
      const v = Math.max(1, Math.min(5, parseInt(maxConcurrentSelect.value, 10) || 3));
      maxConcurrentSelect.value = String(v);
      try {
        await postDownloadSettings({ max_concurrent: v });
      } catch (e) {
        alert(`设置失败：${e.message || e}`);
        await refreshDownloadSettingsFromServer();
      }
    });
    if (autoParseAfterDownloadCheckbox) {
      autoParseAfterDownloadCheckbox.addEventListener("change", async () => {
        const checked = autoParseAfterDownloadCheckbox.checked;
        try {
          await postDownloadSettings({ auto_parse_after_download: checked });
        } catch (e) {
          alert(`设置失败：${e.message || e}`);
          await refreshDownloadSettingsFromServer();
        }
      });
    }
    openNewDownloadModalBtn.addEventListener("click", () => {
      modalNewMatchIdInput.value = "";
      syncDownloadSettingsControls();
      newDownloadModal.classList.add("open");
      setTimeout(() => modalNewMatchIdInput.focus(), 50);
    });
    newDownloadModalCloseBtn.addEventListener("click", () => closeNewDownloadModal());
    cancelNewDownloadBtn.addEventListener("click", () => closeNewDownloadModal());
    newDownloadModal.addEventListener("click", (e) => {
      if (e.target === newDownloadModal) closeNewDownloadModal();
    });
    confirmNewDownloadBtn.addEventListener("click", async () => {
      confirmNewDownloadBtn.disabled = true;
      try {
        const j0 = await fetchDownloadTasksRaw();
        if (j0) lastDownloadPayload = j0;

        const raw = modalNewMatchIdInput.value.trim();
        if (!raw) {
          alert("请填写录像id");
          return;
        }
        const mid = parseInt(raw, 10);
        if (!Number.isFinite(mid) || mid <= 0) {
          alert("请输入有效录像id");
          return;
        }
        if (hasExistingReplayForMatchId(mid)) {
          alert(
            "已存在该录像：列表中已有相同比赛编号的下载任务，或 replays 目录下已有对应 .dem 文件。"
          );
          return;
        }
        const res = await fetch("/api/downloads", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ match_id: mid }),
        });
        const o = await res.json().catch(() => ({}));
        if (!res.ok || !o.ok) throw new Error(o.error || res.statusText);
        closeNewDownloadModal();
        await pollDownloadTasksOnce();
        renderDownloadTaskList();
      } catch (e) {
        alert(`创建任务失败：${e.message || e}`);
      } finally {
        confirmNewDownloadBtn.disabled = false;
      }
    });

    if (batchProcessReplaysBtn) {
      batchProcessReplaysBtn.addEventListener("click", async () => {
        const items = collectReplayManagerCheckedItems();
        if (!items.length) {
          alert("请先勾选要处理的录像。");
          return;
        }
        if (!confirm(`确定对选中项中尚未解析的录像入队解析？共 ${items.length} 条（已解析或已在队列中的会自动跳过）。`)) return;
        try {
          const res = await fetch("/api/downloads/batch/process", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ items }),
          });
          const o = await res.json().catch(() => ({}));
          if (!res.ok || o.ok === false) throw new Error(o.error || res.statusText);
          const r = o.results || [];
          const queued = r.filter((x) => x && x.queued).length;
          const skipped = r.filter((x) => x && x.skipped).length;
          const failed = r.filter((x) => x && x.ok === false).length;
          alert(`批量处理完成：新入队 ${queued} 条，跳过 ${skipped} 条，失败 ${failed} 条。`);
          await pollDownloadTasksOnce();
          renderDownloadTaskList();
        } catch (e) {
          alert(`批量处理失败：${e.message || e}`);
        }
      });
    }
    if (batchClearCacheReplaysBtn) {
      batchClearCacheReplaysBtn.addEventListener("click", () => {
        const items = collectReplayManagerCheckedItems();
        if (!items.length) {
          alert("请先勾选要清除缓存的录像。");
          return;
        }
        openReplayClearCacheModal(items);
      });
    }
    if (clearReplayCacheModalCloseBtn) clearReplayCacheModalCloseBtn.addEventListener("click", closeReplayClearCacheModal);
    if (cancelClearReplayCacheBtn) cancelClearReplayCacheBtn.addEventListener("click", closeReplayClearCacheModal);
    if (clearReplayCacheModal) {
      clearReplayCacheModal.addEventListener("click", (e) => {
        if (e.target === clearReplayCacheModal) closeReplayClearCacheModal();
      });
    }
    if (confirmClearReplayCacheBtn) {
      confirmClearReplayCacheBtn.addEventListener("click", async () => {
        const items = pendingReplayCacheClearItems.slice();
        if (!items.length) {
          closeReplayClearCacheModal();
          return;
        }
        const alsoDeleteDem = Boolean(clearReplayCacheAlsoDeleteDemCheckbox && clearReplayCacheAlsoDeleteDemCheckbox.checked);
        confirmClearReplayCacheBtn.disabled = true;
        try {
          const res = await fetch("/api/downloads/batch/clear-cache", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ items, also_delete_dem: alsoDeleteDem }),
          });
          const o = await res.json().catch(() => ({}));
          if (!res.ok || o.ok === false) throw new Error(o.error || res.statusText);
          const r = o.results || [];
          const okn = r.filter((x) => x && x.ok).length;
          const bad = r.filter((x) => x && x.ok === false).length;
          alert(`清除缓存完成：成功 ${okn} 条，失败 ${bad} 条。`);
          closeReplayClearCacheModal();
          await pollDownloadTasksOnce();
          renderDownloadTaskList();
          if (o.touched_current_replay) {
            try {
              await reloadDataFromServer();
            } catch (err) {
              alert(`列表已更新，但刷新当前画面失败：${err.message || err}`);
            }
          }
        } catch (e) {
          alert(`操作失败：${e.message || e}`);
        } finally {
          confirmClearReplayCacheBtn.disabled = false;
        }
      });
    }

    (async () => {
      // debug+DSR-MAPDBG-01: 记录数据请求与首帧渲染耗时。
      debugLog("data-fetch-start");
      const fetchStartMs = performance.now();
      const res = await fetch("/data", { cache: "no-store" });
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
      if (data.parse_progress) {
        lastDownloadPayload.parse_progress = data.parse_progress;
        if (!Array.isArray(lastDownloadPayload.parse_progress.queued_paths)) {
          lastDownloadPayload.parse_progress.queued_paths = [];
        }
      }
      applyLoadedPayload();
      debugLog("bootstrap-render-called");
    })();

    window.addEventListener("resize", () => {
      if (!data || data.session_ready === false) return;
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
        help="仅用于 --export-json / --no-server 或显式指定文件：回放路径（.dem；本机库仅使用 replays/ 下 .dem）。"
        "启动 Web 时优先 .dsr_last_replay.json；若无记录则默认选用库列表首项（replays/ 下 .dem，"
        "排序与录像管理中本机列表一致），仍仅加载缓存、不自动全量解析。",
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
        print(f"[boot] resolve_input_path：使用显式参数 path={path}")
        return path

    print("[boot] resolve_input_path：未传路径，扫描默认目录 …")
    candidates = iter_default_replay_candidates()
    if not candidates:
        raise FileNotFoundError(
            "未提供 input_replay 且在 replays/ 下找不到 .dem 回放文件。"
            "请用: python3 run.py <your.dem>"
        )
    chosen = candidates[0]
    print(f"[boot] resolve_input_path：选用默认候选第一个 path={chosen}（共 {len(candidates)} 个候选）")
    return chosen


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


LAST_REPLAY_JSON = Path(__file__).resolve().parent / ".dsr_last_replay.json"


def read_last_replay_record() -> tuple[Path, int] | None:
    if not LAST_REPLAY_JSON.is_file():
        print(f"[boot] 上次录像记录文件不存在，跳过: {LAST_REPLAY_JSON}")
        return None
    try:
        raw = json.loads(LAST_REPLAY_JSON.read_text(encoding="utf-8"))
        p = Path(str(raw.get("dem_path", ""))).expanduser()
        fps = int(raw.get("playback_fps", 30) or 30)
        out = (p.resolve(), max(1, fps))
        return out
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
        print(f"[boot] 上次录像记录无效或无法解析 ({type(e).__name__}): {LAST_REPLAY_JSON}")
        return None


def write_last_replay_record(dem_path: Path, playback_fps: int) -> None:
    try:
        LAST_REPLAY_JSON.write_text(
            json.dumps(
                {"dem_path": str(dem_path.resolve()), "playback_fps": int(max(1, playback_fps))},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def make_placeholder_gui_payload(playback_fps: int, *, open_download_manager: bool) -> dict[str, Any]:
    fps = int(max(1, playback_fps))
    return {
        "match_id": 0,
        "game_start_tick": 0,
        "game_end_tick": 0,
        "tick_rate": 30.0,
        "playback_fps": fps,
        "tick_game_time_relation": "(tick - game_start_tick) / tick_rate",
        "map_bounds": {"min_x": 0.0, "max_x": 1.0, "min_y": 0.0, "max_y": 1.0},
        "player_timelines": [],
        "entity_timelines": [],
        "dem_path": "",
        "cache_enabled": False,
        "cache_hit": False,
        "cache_path": "",
        "session_ready": False,
        "open_download_manager": bool(open_download_manager),
    }


def try_load_payload_from_cache_only(replay_path: Path, playback_fps: int) -> tuple[dict[str, Any], Path] | None:
    """仅从解析缓存恢复；不读取/解析 DEM。若无缓存返回 None。"""
    if not replay_path.is_file():
        print(f"[info] 启动仅缓存：录像路径不是文件，跳过: {replay_path}")
        return None
    dem_path = ensure_dem_path(replay_path)
    cache_path = cache_path_for_dem(dem_path)
    cached = load_replay_cache(dem_path)
    if cached is None:
        print(f"[info] 启动仅缓存：无 pkl，将走占位或稍后解析: dem={dem_path} cache={cache_path}")
        return None
    payload = dict(cached)
    payload["playback_fps"] = int(max(playback_fps, 1))
    payload["cache_enabled"] = True
    payload["cache_hit"] = True
    payload["cache_path"] = str(cache_path)
    payload["session_ready"] = True
    payload["open_download_manager"] = False
    print(f"[info] 启动时命中缓存（未解析）: {cache_path}")
    return payload, dem_path


def _startup_replay_path_key(p: Path) -> str:
    try:
        return str(p.resolve()).replace("\\", "/").lower()
    except OSError:
        return str(p).replace("\\", "/").lower()


def iter_ordered_startup_replay_paths(args: argparse.Namespace, rec: tuple[Path, int] | None) -> list[Path]:
    """启动时尝试顺序：命令行仅一项；否则上次记录优先，再按本机库列表顺序去重追加。"""
    out: list[Path] = []
    seen: set[str] = set()

    def add(raw: Path) -> None:
        try:
            pr = raw.expanduser().resolve()
        except OSError:
            return
        k = _startup_replay_path_key(pr)
        if k in seen:
            return
        seen.add(k)
        out.append(pr)

    if args.input_replay:
        add(Path(args.input_replay))
        return out
    if rec is not None:
        add(rec[0])
    for row in list_stored_dem_files():
        try:
            add(Path(str(row.get("path", ""))).expanduser())
        except OSError:
            continue
    return out


def prepare_web_gui_session(args: argparse.Namespace) -> tuple[dict[str, Any], Path, bool]:
    """启动 Web GUI：仅加载 .replay_cache；无命中则占位并打开录像管理。

    多个候选时按 iter_ordered_startup_replay_paths 顺序尝试，直至首个已缓存项。"""
    t_pw = time.perf_counter()
    print("[boot] prepare_web_gui_session：开始（仅 .replay_cache，不解析 DEM）")
    fps = max(1, int(args.fps or 30))
    print(f"[info] Web 会话准备 fps={fps} cli_input_replay={args.input_replay!r}")

    rec: tuple[Path, int] | None = None
    if not args.input_replay:
        rec = read_last_replay_record()
        if rec is not None:
            fps = max(1, rec[1])
            print(f"[info] 上次播放记录: path={rec[0]} fps={fps}")
        else:
            print("[info] 无有效上次录像记录（文件缺失、内容无效或未配置）")
    else:
        print(f"[info] 使用命令行指定的录像路径: {args.input_replay!r}")

    candidates = iter_ordered_startup_replay_paths(args, rec)
    if args.input_replay:
        if not candidates:
            raise FileNotFoundError(f"输入回放不存在: {args.input_replay}")
        if not candidates[0].is_file():
            raise FileNotFoundError(f"输入回放不存在: {candidates[0]}")
        print(f"[info] 使用命令行指定的录像: {candidates[0]}")
    elif not candidates:
        print("[info] 无任何可尝试的录像路径（库目录为空且无记录）")
        print("[boot] 无将预加载的录像，将进入占位会话或自动打开录像管理")
        pl = make_placeholder_gui_payload(fps, open_download_manager=True)
        print(f"[boot] prepare_web_gui_session：结束（占位）({_boot_ms(t_pw):.0f}ms)")
        return pl, replay_storage_root(), True

    first_file: Path | None = None
    for p in candidates:
        if not p.is_file():
            continue
        if first_file is None:
            first_file = p
        hit = try_load_payload_from_cache_only(p, fps)
        if hit is not None:
            payload, dem_path = hit
            write_last_replay_record(dem_path, payload.get("playback_fps", fps))
            if args.input_replay:
                src = "命令行参数"
            elif rec is not None and _startup_replay_path_key(p) == _startup_replay_path_key(rec[0]):
                src = f"上次播放记录（{LAST_REPLAY_JSON.name}）"
            else:
                src = "库列表顺延（首个已生成解析缓存的项）"
            print(f"[boot] 将被加载的录像: {p}（来源：{src}）")
            print(f"[info] Web 启动载入缓存完毕 match_id={payload.get('match_id')} dem={dem_path}")
            print(f"[boot] prepare_web_gui_session：结束（命中缓存）({_boot_ms(t_pw):.0f}ms)")
            return payload, dem_path.resolve(), False

    hint = replay_storage_root()
    if first_file is not None:
        try:
            hint = ensure_dem_path(first_file).resolve()
        except OSError:
            try:
                hint = first_file.resolve()
            except OSError:
                hint = first_file
        print(f"[info] 候选录像均无解析缓存，请在录像管理中先「处理」。参考路径: {first_file}")
    else:
        print("[info] 候选路径均非有效文件，占位")
    pl = make_placeholder_gui_payload(fps, open_download_manager=True)
    print(f"[info] 占位会话 open_dm=True dem_hint={hint}")
    print(f"[boot] prepare_web_gui_session：结束（占位，待录像管理）({_boot_ms(t_pw):.0f}ms)")
    return pl, hint, True


def _collect_parse_with_progress(
    dem_path: Path,
    estimated_end_tick: int,
    on_tick_progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
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
        if cancel_check is not None and cancel_check():
            raise ParseCancelledError()
        nonlocal last_report_tick
        tick = int(parser.tick)
        if tick - last_report_tick < 180:
            return
        last_report_tick = tick
        if on_tick_progress is not None:
            on_tick_progress(tick, progress_total)
        _print_parse_progress(tick, progress_total, done=False)

    parser.on_entity(_progress_callback)
    _print_parse_progress(0, progress_total, done=False)
    if on_tick_progress is not None:
        on_tick_progress(0, progress_total)
    if cancel_check is not None and cancel_check():
        raise ParseCancelledError()
    parser.parse()
    final_tick = min(max(int(parser.tick), 0), progress_total)
    _print_parse_progress(final_tick, progress_total, done=True)
    if on_tick_progress is not None:
        on_tick_progress(final_tick, progress_total)
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


class ReplayCacheMissingError(Exception):
    """Web 播放/载入仅允许使用 .replay_cache；未命中缓存时抛出。"""


def build_gui_payload(
    replay_path: Path,
    playback_fps: int,
    *,
    parse_progress_cb: Callable[[int, int], None] | None = None,
    allow_parse: bool = True,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[dict[str, Any], Path]:
    print(f"[info] 读取回放: {replay_path}")
    dem_path = ensure_dem_path(replay_path)
    cache_path = cache_path_for_dem(dem_path)
    cached = load_replay_cache(dem_path)
    if cached is not None:
        payload = dict(cached)
        payload["playback_fps"] = int(max(playback_fps, 1))
        payload["cache_enabled"] = True
        payload["cache_hit"] = True
        payload["cache_path"] = str(cache_path)
        payload["session_ready"] = True
        payload["open_download_manager"] = False
        print(f"[info] 命中缓存: {cache_path}")
        return payload, dem_path

    if not allow_parse:
        raise ReplayCacheMissingError(
            "该录像尚无解析缓存；播放与载入仅使用已解析数据。请在录像管理中点击「处理」生成缓存后再试。"
            f" dem={dem_path} cache={cache_path}"
        )

    print(f"[info] 解析 DEM: {dem_path}")
    print(f"[info] 缓存未命中，开始 gem 元数据 + 全量 tick 解析: cache_path={cache_path}")
    match = gem.parse(str(dem_path))
    est = max(int(match.game_end_tick), 1)
    try:
        parser, player_ext, world_ext = _collect_parse_with_progress(
            dem_path,
            estimated_end_tick=est,
            on_tick_progress=parse_progress_cb,
            cancel_check=cancel_check,
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
        payload["session_ready"] = True
        payload["open_download_manager"] = False
        print(f"[info] 已写入缓存: {cache_path}")
        print(
            f"[info] 回放范围: 0 -> {payload['game_end_tick']} (game_start_tick={payload['game_start_tick']}), "
            f"tick_rate={payload['tick_rate']:.2f}, 玩家轨迹={len(payload['player_timelines'])}, "
            f"世界实体轨迹={len(payload['entity_timelines'])}"
        )
        return payload, dem_path
    except ParseCancelledError:
        try:
            delete_replay_cache(dem_path)
        except OSError:
            pass
        print(f"[info] 解析已取消，已尝试清理缓存: dem={dem_path}")
        raise
    finally:
        if parse_progress_cb is not None:
            try:
                parse_progress_cb(-1, -1)
            except Exception:
                pass


def library_file_playback_ready(path_str: str) -> bool:
    """本机库 .dem 路径是否已有可复用的解析缓存。"""
    try:
        p = Path(str(path_str))
        if not p.is_file():
            return False
        if p.suffix.lower() != ".dem" or p.name.lower().endswith(".dem.bz2"):
            return False
        return cache_path_for_dem(p).exists()
    except OSError:
        return False


def annotate_local_replays_with_cache(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for it in rows:
        it["playback_ready"] = library_file_playback_ready(str(it.get("path", "")))
        try:
            dp = Path(str(it.get("path", ""))).expanduser().resolve()
            it["dem_norm_key"] = norm_dem_storage_key(dp)
        except OSError:
            it["dem_norm_key"] = norm_dem_storage_key(Path(str(it.get("path", ""))))
    return rows


def enrich_download_task(task: dict[str, Any]) -> dict[str, Any]:
    t = dict(task)
    t["playback_ready"] = False
    t["cache_exists"] = False
    t["output_size"] = None
    t["dem_norm_key"] = None
    op = task.get("output_dem_path")
    if task.get("state") == "completed" and op:
        dem = Path(str(op))
        if dem.is_file():
            try:
                t["cache_exists"] = cache_path_for_dem(dem).exists()
                t["playback_ready"] = t["cache_exists"]
                t["output_size"] = int(dem.stat().st_size)
                t["dem_norm_key"] = norm_dem_storage_key(dem)
            except OSError:
                t["dem_norm_key"] = norm_dem_storage_key(dem)
    return t


def run_server(host: str, port: int, payload: dict[str, Any], dem_path: Path, open_browser: bool) -> None:
    t_rs = time.perf_counter()
    try:
        dp0 = dem_path.resolve()
    except OSError:
        dp0 = dem_path
    print(
        f"[boot] run_server：开始 host={host!r} port={port} open_browser={open_browser} "
        f"session_ready={payload.get('session_ready', True)} "
        f"将被加载/当前会话 dem={dp0}"
    )
    ctx: dict[str, Any] = {"payload": payload, "dem_path": Path(dp0)}
    lock = threading.Lock()
    download_mgr = DownloadTaskManager()
    print(f"[boot] run_server：DownloadTaskManager 已创建 ({_boot_ms(t_rs):.0f}ms)")
    print(
        f"[info] Web 服务初始状态 dem={dp0} session_ready={payload.get('session_ready', True)} "
        f"match_id={payload.get('match_id')} open_download_manager={payload.get('open_download_manager')}"
    )
    print(f"[boot] run_server：上下文与解析进度状态已初始化 ({_boot_ms(t_rs):.0f}ms)")

    remarks_lock = threading.Lock()
    user_remarks: dict[str, dict[str, str]] = _load_user_remarks()

    parse_progress: dict[str, Any] = {"active": False, "dem_path": None, "pct": 0.0}
    parse_worker_stop = threading.Event()
    parse_q_lock = threading.Lock()
    parse_q_cv = threading.Condition(parse_q_lock)
    parse_deque: deque[tuple[Path, str]] = deque()
    parse_q_set: set[str] = set()
    parse_cancel_flag = [False]

    def parse_progress_for_client() -> dict[str, Any]:
        with parse_q_lock:
            queued_paths = [k for _, k in parse_deque]
        return {
            "active": bool(parse_progress["active"]),
            "dem_path": parse_progress["dem_path"],
            "pct": float(parse_progress["pct"] or 0.0),
            "queued_paths": queued_paths,
        }

    def parse_progress_cb_for_replay(replay_p: Path) -> Callable[[int, int], None]:
        dem_ref = ensure_dem_path(replay_p)
        key = norm_dem_storage_key(dem_ref)
        last_log_bucket = [-1]

        def cb(cur: int, tot: int) -> None:
            if cur < 0 or tot < 0:
                if parse_progress["active"]:
                    print(f"[web] 解析进度回调结束 dem={key} 收尾前 pct={parse_progress['pct']:.1f}%")
                last_log_bucket[0] = -1
                parse_progress["active"] = False
                parse_progress["pct"] = 0.0
                parse_progress["dem_path"] = None
                return
            parse_progress["active"] = True
            parse_progress["dem_path"] = key
            parse_progress["pct"] = min(99.0, 100.0 * float(cur) / max(float(tot), 1.0))
            b = int(parse_progress["pct"] // 10)
            if b != last_log_bucket[0]:
                last_log_bucket[0] = b
                print(f"[web] 解析进度约 {parse_progress['pct']:.0f}% tick={cur}/{tot} dem={key}")

        return cb

    def enqueue_parse_request(replay_path: Path) -> dict[str, Any]:
        dem_ref = ensure_dem_path(replay_path)
        key = norm_dem_storage_key(dem_ref)
        with parse_q_lock:
            if bool(parse_progress["active"]) and parse_progress["dem_path"] == key:
                return {"ok": True, "queued": False, "duplicate": True}
            if key in parse_q_set:
                return {"ok": True, "queued": False, "duplicate": True}
            parse_deque.append((dem_ref, key))
            parse_q_set.add(key)
            parse_q_cv.notify()
        return {"ok": True, "queued": True, "duplicate": False}

    def cancel_parse_for_key(norm_key: str) -> dict[str, Any]:
        nk = (norm_key or "").strip().lower()
        removed_queue = False
        cancelled_active = False
        with parse_q_lock:
            if nk in parse_q_set:
                newd: deque[tuple[Path, str]] = deque()
                for p_item, k_item in parse_deque:
                    if k_item != nk:
                        newd.append((p_item, k_item))
                    else:
                        removed_queue = True
                parse_deque.clear()
                parse_deque.extend(newd)
                parse_q_set.discard(nk)
            if bool(parse_progress["active"]) and parse_progress["dem_path"] == nk:
                parse_cancel_flag[0] = True
                cancelled_active = True
        return {"ok": True, "removed_queue": removed_queue, "cancelled_active": cancelled_active}

    def parse_worker() -> None:
        while not parse_worker_stop.is_set():
            with parse_q_lock:
                while not parse_deque and not parse_worker_stop.is_set():
                    parse_q_cv.wait(timeout=0.3)
                if parse_worker_stop.is_set() and not parse_deque:
                    return
                if not parse_deque:
                    continue
                dem_item, key = parse_deque.popleft()
                parse_q_set.discard(key)
                parse_cancel_flag[0] = False
                parse_progress["active"] = True
                parse_progress["dem_path"] = key
                parse_progress["pct"] = 0.0
            pc = parse_progress_cb_for_replay(dem_item)
            try:
                build_gui_payload(
                    dem_item,
                    playback_fps=_current_fps(),
                    parse_progress_cb=pc,
                    allow_parse=True,
                    cancel_check=lambda: parse_cancel_flag[0],
                )
            except ParseCancelledError:
                print(f"[web] 解析已取消 dem={key}")
            except Exception as ex:
                print(f"[web] 解析线程异常 dem={key}: {ex}")
            finally:
                parse_cancel_flag[0] = False
                try:
                    pc(-1, -1)
                except Exception:
                    pass

    threading.Thread(target=parse_worker, name="parse-queue", daemon=True).start()

    def _auto_parse_after_download_fn(dem_p: Path) -> None:
        enqueue_parse_request(dem_p)

    download_mgr.set_auto_parse_callback(_auto_parse_after_download_fn)

    def _current_fps() -> int:
        with lock:
            return int(ctx["payload"].get("playback_fps", 30) or 30)

    def _apply_loaded_replay_payload(new_payload: dict[str, Any], new_dem: Path, fallback_fps: int) -> tuple[str, Any]:
        with lock:
            new_payload["open_download_manager"] = False
            ctx["payload"] = new_payload
            ctx["dem_path"] = new_dem.resolve()
            applied_fps = int(new_payload.get("playback_fps", fallback_fps) or fallback_fps)
            write_last_replay_record(ctx["dem_path"], applied_fps)
            dem_out = str(ctx["dem_path"])
        return dem_out, new_payload["match_id"]

    def _switch_replay_session(replay_path: Path, *, allow_parse: bool) -> tuple[str, Any]:
        fps = _current_fps()
        new_payload, new_dem = build_gui_payload(
            replay_path,
            playback_fps=fps,
            parse_progress_cb=parse_progress_cb_for_replay(replay_path),
            allow_parse=allow_parse,
        )
        return _apply_loaded_replay_payload(new_payload, new_dem, fps)

    def _enqueue_process_replay(replay_path: Path) -> dict[str, Any]:
        return enqueue_parse_request(replay_path)

    def _read_local_library_path(body: dict[str, Any], deny_error: str) -> Path:
        p = Path(str(body.get("path", ""))).expanduser()
        try:
            p = p.resolve()
        except OSError as ex:
            raise ValueError("路径无效") from ex
        if not is_replay_library_path(p):
            raise ValueError(deny_error)
        return p

    def _read_task_dem_path(tid: str, *, only_completed: bool) -> Path:
        with lock:
            tasks = {t["id"]: t for t in download_mgr.list_tasks()}
            info = tasks.get(tid)
            if only_completed:
                if not info or info.get("state") != "completed" or not info.get("output_dem_path"):
                    raise ValueError("仅已完成任务可生成缓存")
            else:
                if not info or not info.get("output_dem_path"):
                    raise ValueError("任务未完成或无文件")
            dem_file = Path(str(info["output_dem_path"]))
        if not dem_file.is_file():
            raise FileNotFoundError("录像文件不存在")
        return dem_file

    def _batch_item_resolve_dem(item: dict[str, Any]) -> tuple[Path | None, str | None]:
        k = item.get("kind")
        if k == "local":
            try:
                p = _read_local_library_path({"path": item.get("path")}, "仅允许处理 replays/ 下的 .dem")
            except ValueError as e:
                return None, str(e)
            if not p.is_file():
                return None, "文件不存在"
            return p, None
        if k == "task":
            tid = str(item.get("task_id", "")).strip()
            if not tid:
                return None, "缺少 task_id"
            try:
                return _read_task_dem_path(tid, only_completed=True), None
            except ValueError as e:
                return None, str(e)
            except FileNotFoundError:
                return None, "录像文件不存在"
        return None, "条目 kind 无效"

    def _dem_matches_session_dem(dem: Path) -> bool:
        with lock:
            cur = ctx["dem_path"]
        try:
            return dem.resolve() == cur.resolve()
        except OSError:
            return str(dem) == str(cur)

    def current_payload_bytes() -> bytes:
        out = dict(ctx["payload"])
        out["parse_progress"] = parse_progress_for_client()
        return json.dumps(out, ensure_ascii=False).encode("utf-8")

    def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        n = int(handler.headers.get("Content-Length", "0") or "0")
        raw = handler.rfile.read(n) if n > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))

    def _send_json(handler: BaseHTTPRequestHandler, code: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    print(f"[boot] run_server：正在编码内置 HTML 模板 ({_boot_ms(t_rs):.0f}ms)")
    html_bytes = HTML_TEMPLATE.encode("utf-8")
    print(f"[boot] run_server：HTML 模板就绪 len={len(html_bytes)} ({_boot_ms(t_rs):.0f}ms)")
    # debug+DSR-MAPDBG-01: 服务端静态底图读取与请求日志，定位是否卡在图片传输。
    map_bg_path = Path(__file__).resolve().parent / "assets" / "maps" / "map_full.png"
    map_bg_bytes = map_bg_path.read_bytes() if map_bg_path.exists() else None
    print(
        f"[debug+DSR-MAPDBG-01] map-bg-init path={map_bg_path} "
        f"exists={map_bg_path.exists()} size={0 if map_bg_bytes is None else len(map_bg_bytes)}"
    )
    print(f"[boot] run_server：底图资源已加载 ({_boot_ms(t_rs):.0f}ms)")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            req_path = urlparse(self.path).path
            if req_path == "/api/downloads":
                with remarks_lock:
                    r_copy = {
                        "by_path": dict(user_remarks["by_path"]),
                        "by_task": dict(user_remarks["by_task"]),
                    }
                tasks: list[dict[str, Any]] = []
                for t in download_mgr.list_tasks():
                    td = enrich_download_task(t)
                    tid = str(td.get("id", ""))
                    dk = td.get("dem_norm_key")
                    td["user_remark"] = (r_copy["by_task"].get(tid, "") or (r_copy["by_path"].get(dk, "") if dk else ""))[
                        :2000
                    ]
                    tasks.append(td)
                locals_raw = annotate_local_replays_with_cache(list_stored_dem_files())
                for row in locals_raw:
                    rk = row.get("dem_norm_key")
                    row["user_remark"] = (r_copy["by_path"].get(rk, "") if rk else "")[:2000]
                _send_json(
                    self,
                    200,
                    {
                        "tasks": tasks,
                        "max_concurrent": download_mgr.get_max_concurrent(),
                        "auto_parse_after_download": download_mgr.get_auto_parse_after_download(),
                        "local_replays": locals_raw,
                        "parse_progress": parse_progress_for_client(),
                        "storage_roots": {
                            "replays": str(replay_storage_root().resolve()),
                        },
                    },
                )
                return
            if self.path == "/" or self.path.startswith("/?"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.end_headers()
                self.wfile.write(html_bytes)
                return
            if req_path == "/data":
                payload_bytes = current_payload_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Content-Length", str(len(payload_bytes)))
                self.end_headers()
                self.wfile.write(payload_bytes)
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
            req_path = urlparse(self.path).path
            if req_path == "/api/downloads/local/load":
                print(f"[http] POST {req_path} client={self.client_address[0]}")
                try:
                    body = _read_json_body(self)
                    p = _read_local_library_path(body, "仅允许载入 replays/ 下的 .dem 录像")
                    dem_out, match_id = _switch_replay_session(p, allow_parse=False)
                    print(f"[http] local/load 成功 match_id={match_id} dem={dem_out}")
                    _send_json(self, 200, {"ok": True, "dem_path": dem_out, "match_id": match_id})
                except ValueError as e:
                    print(f"[http] local/load 参数错误: {e}")
                    _send_json(self, 400, {"ok": False, "error": str(e)})
                except ReplayCacheMissingError as e:
                    print(f"[http] local/load 无可用缓存: {e}")
                    _send_json(self, 400, {"ok": False, "error": str(e)})
                except Exception as e:
                    print(f"[http] local/load 失败: {e}")
                    _send_json(self, 502, {"ok": False, "error": str(e)})
                return
            if req_path == "/api/downloads/batch/clear-cache":
                print(f"[http] POST {req_path} client={self.client_address[0]}")
                try:
                    body = _read_json_body(self)
                    raw_items = body.get("items")
                    if not isinstance(raw_items, list):
                        raise ValueError("items 必须是数组")
                    also_delete_dem = bool(body.get("also_delete_dem"))
                    results: list[dict[str, Any]] = []
                    touched_current = False
                    for raw in raw_items:
                        if not isinstance(raw, dict):
                            results.append({"ok": False, "error": "条目格式错误"})
                            continue
                        dem, err = _batch_item_resolve_dem(raw)
                        if err or dem is None:
                            results.append({"ok": False, "error": err or "无法解析路径"})
                            continue
                        path_s = str(dem)
                        try:
                            delete_replay_cache(dem)
                        except OSError as oe:
                            results.append({"ok": False, "error": f"删除缓存失败: {oe}", "path": path_s})
                            continue
                        if _dem_matches_session_dem(dem):
                            touched_current = True
                        entry: dict[str, Any] = {"ok": True, "path": path_s, "cache_cleared": True}
                        if also_delete_dem:
                            k = raw.get("kind")
                            if k == "local":
                                purge_local_replay_dem_file(dem)
                                entry["dem_deleted"] = True
                            elif k == "task":
                                tid = str(raw.get("task_id", "")).strip()
                                try:
                                    download_mgr.delete_task(tid)
                                except KeyError:
                                    results.append({"ok": False, "error": "任务不存在", "path": path_s})
                                    continue
                                entry["dem_deleted"] = True
                        results.append(entry)
                    if touched_current:
                        with lock:
                            ctx["payload"]["cache_hit"] = False
                    _send_json(self, 200, {"ok": True, "results": results, "touched_current_replay": touched_current})
                except ValueError as e:
                    _send_json(self, 400, {"ok": False, "error": str(e)})
                except Exception as e:
                    print(f"[http] batch/clear-cache 失败: {e}")
                    _send_json(self, 502, {"ok": False, "error": str(e)})
                return
            if req_path == "/api/downloads/batch/process":
                print(f"[http] POST {req_path} client={self.client_address[0]}")
                try:
                    body = _read_json_body(self)
                    raw_items = body.get("items")
                    if not isinstance(raw_items, list):
                        raise ValueError("items 必须是数组")
                    results: list[dict[str, Any]] = []
                    for raw in raw_items:
                        if not isinstance(raw, dict):
                            results.append({"ok": False, "error": "条目格式错误"})
                            continue
                        dem, err = _batch_item_resolve_dem(raw)
                        if err or dem is None:
                            results.append({"ok": False, "error": err or "无法解析路径"})
                            continue
                        path_s = str(dem)
                        if library_file_playback_ready(path_s):
                            results.append({"ok": True, "skipped": True, "reason": "已有解析缓存", "path": path_s})
                            continue
                        out = enqueue_parse_request(dem)
                        if out.get("duplicate"):
                            results.append({"ok": True, "skipped": True, "reason": "已在队列或正在解析", "path": path_s})
                        else:
                            results.append({"ok": True, "queued": True, "path": path_s})
                    _send_json(self, 200, {"ok": True, "results": results})
                except ValueError as e:
                    _send_json(self, 400, {"ok": False, "error": str(e)})
                except Exception as e:
                    print(f"[http] batch/process 失败: {e}")
                    _send_json(self, 502, {"ok": False, "error": str(e)})
                return
            if req_path == "/api/downloads/parse/cancel":
                print(f"[http] POST {req_path} client={self.client_address[0]}")
                try:
                    body = _read_json_body(self)
                    nk = body.get("dem_norm_key")
                    if isinstance(nk, str) and nk.strip():
                        out = cancel_parse_for_key(nk.strip())
                    elif body.get("task_id"):
                        dem_file = _read_task_dem_path(str(body["task_id"]), only_completed=True)
                        out = cancel_parse_for_key(norm_dem_storage_key(ensure_dem_path(dem_file)))
                    else:
                        raise ValueError("请提供 dem_norm_key 或 task_id")
                    _send_json(self, 200, {"ok": True, **out})
                except ValueError as e:
                    _send_json(self, 400, {"ok": False, "error": str(e)})
                except Exception as e:
                    print(f"[http] parse/cancel 失败: {e}")
                    _send_json(self, 502, {"ok": False, "error": str(e)})
                return
            if req_path == "/api/downloads/local/remark":
                print(f"[http] POST {req_path} client={self.client_address[0]}")
                try:
                    body = _read_json_body(self)
                    p = _read_local_library_path(body, "仅允许为 replays/ 下的 .dem 设置备注")
                    remark = body.get("user_remark", "")
                    if not isinstance(remark, str):
                        raise ValueError("user_remark 必须是字符串")
                    key = norm_dem_storage_key(ensure_dem_path(p))
                    with remarks_lock:
                        user_remarks["by_path"][key] = remark.strip()[:2000]
                        _save_user_remarks(user_remarks)
                    _send_json(self, 200, {"ok": True})
                except ValueError as e:
                    _send_json(self, 400, {"ok": False, "error": str(e)})
                except Exception as e:
                    print(f"[http] local/remark 失败: {e}")
                    _send_json(self, 502, {"ok": False, "error": str(e)})
                return
            if req_path == "/api/downloads/local/process":
                print(f"[http] POST {req_path} client={self.client_address[0]}")
                try:
                    body = _read_json_body(self)
                    p = _read_local_library_path(body, "仅允许处理 replays/ 下的 .dem")
                    out = _enqueue_process_replay(p)
                    print(f"[http] local/process 入队 path={p} out={out}")
                    _send_json(self, 200, {"ok": True, **out})
                except ValueError as e:
                    print(f"[http] local/process 参数错误: {e}")
                    _send_json(self, 400, {"ok": False, "error": str(e)})
                except Exception as e:
                    print(f"[http] local/process 失败: {e}")
                    _send_json(self, 502, {"ok": False, "error": str(e)})
                return
            if req_path == "/api/downloads/local/play":
                print(f"[http] POST {req_path} client={self.client_address[0]}")
                try:
                    body = _read_json_body(self)
                    p = _read_local_library_path(body, "仅允许播放 replays/ 下的 .dem")
                    dem_out, match_id = _switch_replay_session(p, allow_parse=False)
                    print(f"[http] local/play 成功 match_id={match_id} dem={dem_out}")
                    _send_json(self, 200, {"ok": True, "dem_path": dem_out, "match_id": match_id})
                except ValueError as e:
                    print(f"[http] local/play 参数错误: {e}")
                    _send_json(self, 400, {"ok": False, "error": str(e)})
                except ReplayCacheMissingError as e:
                    print(f"[http] local/play 无可用缓存: {e}")
                    _send_json(self, 400, {"ok": False, "error": str(e)})
                except Exception as e:
                    print(f"[http] local/play 失败: {e}")
                    _send_json(self, 502, {"ok": False, "error": str(e)})
                return
            if req_path == "/api/downloads":
                print(f"[http] POST {req_path} client={self.client_address[0]}")
                try:
                    body = _read_json_body(self)
                    mid = int(body.get("match_id", 0))
                    task = download_mgr.create_task(mid, None)
                    print(f"[http] 已创建下载任务 id={task.id} match_id={mid}")
                    _send_json(self, 200, {"ok": True, "task": task.to_dict()})
                except (ValueError, TypeError) as e:
                    print(f"[http] 创建下载任务参数错误: {e}")
                    _send_json(self, 400, {"ok": False, "error": str(e)})
                except Exception as e:
                    print(f"[http] 创建下载任务失败: {e}")
                    _send_json(self, 502, {"ok": False, "error": str(e)})
                return
            if req_path == "/api/downloads/settings":
                print(f"[http] POST {req_path} client={self.client_address[0]}")
                try:
                    body = _read_json_body(self)
                    if "max_concurrent" not in body and "auto_parse_after_download" not in body:
                        _send_json(
                            self,
                            400,
                            {"ok": False, "error": "请提供 max_concurrent 或 auto_parse_after_download"},
                        )
                        return
                    if "max_concurrent" in body:
                        mc = int(body["max_concurrent"])
                        download_mgr.set_max_concurrent(mc)
                        print(f"[http] 下载并发上限已设为 max_concurrent={download_mgr.get_max_concurrent()}")
                    if "auto_parse_after_download" in body:
                        download_mgr.set_auto_parse_after_download(bool(body["auto_parse_after_download"]))
                    _send_json(
                        self,
                        200,
                        {
                            "ok": True,
                            "max_concurrent": download_mgr.get_max_concurrent(),
                            "auto_parse_after_download": download_mgr.get_auto_parse_after_download(),
                        },
                    )
                except (ValueError, TypeError) as e:
                    print(f"[http] 下载设置参数错误: {e}")
                    _send_json(self, 400, {"ok": False, "error": str(e)})
                except Exception as e:
                    print(f"[http] 下载设置失败: {e}")
                    _send_json(self, 502, {"ok": False, "error": str(e)})
                return
            sub = req_path.removeprefix("/api/downloads/").strip("/")
            if sub:
                parts = sub.split("/")
                tid = parts[0]
                action = parts[1] if len(parts) > 1 else None
                if action == "pause":
                    print(f"[http] POST /api/downloads/{tid}/pause client={self.client_address[0]}")
                    try:
                        download_mgr.pause(tid)
                        print(f"[http] 任务已暂停 id={tid}")
                        _send_json(self, 200, {"ok": True})
                    except KeyError:
                        print(f"[http] pause 任务不存在 id={tid}")
                        _send_json(self, 404, {"ok": False, "error": "任务不存在"})
                    return
                if action == "resume":
                    print(f"[http] POST /api/downloads/{tid}/resume client={self.client_address[0]}")
                    try:
                        download_mgr.resume(tid)
                        print(f"[http] 任务已继续 id={tid}")
                        _send_json(self, 200, {"ok": True})
                    except KeyError:
                        print(f"[http] resume 任务不存在 id={tid}")
                        _send_json(self, 404, {"ok": False, "error": "任务不存在"})
                    return
                if action == "process":
                    print(f"[http] POST /api/downloads/{tid}/process client={self.client_address[0]}")
                    try:
                        dem_file = _read_task_dem_path(tid, only_completed=True)
                        out = _enqueue_process_replay(dem_file)
                        print(f"[http] 任务 process 入队 id={tid} dem={dem_file} out={out}")
                        _send_json(self, 200, {"ok": True, **out})
                    except ValueError as e:
                        _send_json(self, 400, {"ok": False, "error": str(e)})
                    except FileNotFoundError as e:
                        _send_json(self, 404, {"ok": False, "error": str(e)})
                    except Exception as e:
                        print(f"[http] 任务 process 失败 id={tid}: {e}")
                        _send_json(self, 502, {"ok": False, "error": str(e)})
                    return
                if action == "play":
                    print(f"[http] POST /api/downloads/{tid}/play client={self.client_address[0]}")
                    try:
                        dem_file = _read_task_dem_path(tid, only_completed=False)
                        dem_out, match_id = _switch_replay_session(dem_file, allow_parse=False)
                        print(f"[http] 任务 play 成功 id={tid} match_id={match_id} dem={dem_out}")
                        _send_json(self, 200, {"ok": True, "dem_path": dem_out, "match_id": match_id})
                    except ValueError as e:
                        _send_json(self, 400, {"ok": False, "error": str(e)})
                    except FileNotFoundError as e:
                        _send_json(self, 404, {"ok": False, "error": str(e)})
                    except ReplayCacheMissingError as e:
                        print(f"[http] 任务 play 无可用缓存 id={tid}: {e}")
                        _send_json(self, 400, {"ok": False, "error": str(e)})
                    except Exception as e:
                        print(f"[http] 任务 play 失败 id={tid}: {e}")
                        _send_json(self, 502, {"ok": False, "error": str(e)})
                    return
                if action == "load":
                    print(f"[http] POST /api/downloads/{tid}/load client={self.client_address[0]}")
                    try:
                        dem_file = _read_task_dem_path(tid, only_completed=False)
                        dem_out, match_id = _switch_replay_session(dem_file, allow_parse=False)
                        print(f"[http] 任务 load 成功 id={tid} match_id={match_id} dem={dem_out}")
                        _send_json(self, 200, {"ok": True, "dem_path": dem_out, "match_id": match_id})
                    except ValueError as e:
                        _send_json(self, 400, {"ok": False, "error": str(e)})
                    except FileNotFoundError as e:
                        _send_json(self, 404, {"ok": False, "error": str(e)})
                    except ReplayCacheMissingError as e:
                        print(f"[http] 任务 load 无可用缓存 id={tid}: {e}")
                        _send_json(self, 400, {"ok": False, "error": str(e)})
                    except Exception as e:
                        print(f"[http] 任务 load 失败 id={tid}: {e}")
                        _send_json(self, 502, {"ok": False, "error": str(e)})
                    return
            self.send_response(404)
            self.end_headers()

        def do_PATCH(self) -> None:  # noqa: N802
            req_path = urlparse(self.path).path
            prefix = "/api/downloads/"
            if not req_path.startswith(prefix):
                self.send_response(404)
                self.end_headers()
                return
            tid = req_path[len(prefix) :].strip("/").split("/")[0]
            if not tid:
                self.send_response(404)
                self.end_headers()
                return
            try:
                print(f"[http] PATCH {req_path} client={self.client_address[0]}")
                body = _read_json_body(self)
                did = False
                if "user_remark" in body:
                    remark = body["user_remark"]
                    if not isinstance(remark, str):
                        raise ValueError("user_remark 必须是字符串")
                    with remarks_lock:
                        user_remarks["by_task"][tid] = remark.strip()[:2000]
                        _save_user_remarks(user_remarks)
                    did = True
                if "display_stem" in body or "name" in body:
                    stem = body.get("display_stem") or body.get("name")
                    if not isinstance(stem, str):
                        raise ValueError("display_stem 必须是字符串")
                    download_mgr.update_display_stem(tid, stem)
                    print(f"[http] 任务重命名 id={tid} stem={stem!r}")
                    did = True
                if not did:
                    raise ValueError("请提供 user_remark 或 display_stem")
                _send_json(self, 200, {"ok": True})
            except KeyError:
                print(f"[http] PATCH 任务不存在 id={tid}")
                _send_json(self, 404, {"ok": False, "error": "任务不存在"})
            except Exception as e:
                print(f"[http] PATCH 失败 id={tid}: {e}")
                _send_json(self, 400, {"ok": False, "error": str(e)})

        def do_DELETE(self) -> None:  # noqa: N802
            req_path = urlparse(self.path).path
            prefix = "/api/downloads/"
            if not req_path.startswith(prefix):
                self.send_response(404)
                self.end_headers()
                return
            tid = req_path[len(prefix) :].strip("/").split("/")[0]
            if not tid:
                self.send_response(404)
                self.end_headers()
                return
            try:
                print(f"[http] DELETE /api/downloads/{tid} client={self.client_address[0]}")
                download_mgr.delete_task(tid)
                print(f"[http] 任务已删除 id={tid}")
                _send_json(self, 200, {"ok": True})
            except KeyError:
                print(f"[http] DELETE 任务不存在 id={tid}")
                _send_json(self, 404, {"ok": False, "error": "任务不存在"})

        def log_message(self, format_str: str, *args: Any) -> None:
            return

    print(f"[boot] run_server：正在绑定 TCP {host}:{port} … ({_boot_ms(t_rs):.0f}ms)")
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"[boot] run_server：绑定失败 {host}:{port} → {e}")
        raise
    url = f"http://{host}:{port}/"
    print(f"[boot] run_server：HTTP 监听已就绪 {url}（自 run_server 起 {_boot_ms(t_rs):.0f}ms）")
    print(f"[done] GUI 地址: {url}")
    if open_browser:
        print("[boot] run_server：正在请求系统默认浏览器打开页面 …")
        webbrowser.open(url)
    print(f"[boot] run_server：进入 serve_forever，服务已对外可用（{_boot_ms(t_rs):.0f}ms，Ctrl+C 退出）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[boot] 收到 KeyboardInterrupt，正在退出 serve_forever …")
    finally:
        server.server_close()
        print("[boot] HTTP server 已关闭")


def main() -> None:
    t0 = time.perf_counter()
    print(f"[boot] DotaSimpleReplay 启动 pid={os.getpid()} argv={sys.argv!r}")
    args = parse_args()
    migrate_legacy_replay_samples_to_replays()
    extract_replays_bz2_archives()
    print(
        f"[boot] 参数解析完成 (+{_boot_ms(t0):.0f}ms) host={args.host!r} port={args.port} fps={args.fps} "
        f"export_json={args.export_json!r} no_server={args.no_server} no_open_browser={args.no_open_browser} "
        f"input_replay={args.input_replay!r}"
    )
    launch_payload: tuple[dict[str, Any], Path] | None = None

    if args.export_json:
        print(f"[boot] 开始 export-json 流程 → {args.export_json!r} (+{_boot_ms(t0):.0f}ms)")
        print("[boot] 解析输入回放路径（resolve_input_path）…")
        replay_path = resolve_input_path(args.input_replay)
        print(f"[boot] 输入路径: {replay_path} (+{_boot_ms(t0):.0f}ms)")
        print("[boot] 正在 build_gui_payload（可能较久）…")
        payload, dem_path = build_gui_payload(replay_path, playback_fps=args.fps)
        print(f"[boot] build_gui_payload 完成 match_id={payload.get('match_id')} (+{_boot_ms(t0):.0f}ms)")
        out = Path(args.export_json).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[done] 已导出 GUI 数据: {out} (+{_boot_ms(t0):.0f}ms)")
        if not args.no_server:
            launch_payload = (payload, dem_path)
            print(f"[boot] 将在同一解析结果上启动 Web（+{_boot_ms(t0):.0f}ms）")

    if args.no_server:
        print(f"[boot] --no_server：将不启动 Web (+{_boot_ms(t0):.0f}ms)")
        if not args.export_json:
            print("[boot] 仅解析模式：resolve_input_path …")
            replay_path = resolve_input_path(args.input_replay)
            print(f"[boot] 仅解析模式：build_gui_payload … path={replay_path}")
            build_gui_payload(replay_path, playback_fps=args.fps)
            print(f"[boot] 仅解析模式结束 (+{_boot_ms(t0):.0f}ms)")
        return

    if launch_payload is not None:
        payload, dem_path = launch_payload
        print(f"[boot] 将被加载的录像（export-json 已解析）: dem={dem_path}")
        print(f"[info] 使用 export-json 解析结果启动 Web dem={dem_path}")
    else:
        print(f"[boot] 调用 prepare_web_gui_session（快速路径，不解析 DEM）(+{_boot_ms(t0):.0f}ms)")
        payload, dem_path, _ = prepare_web_gui_session(args)
        print(f"[info] 常规 Web 启动 dem={dem_path} session_ready={payload.get('session_ready', True)}")

    print(f"[boot] 即将进入 run_server（累计 +{_boot_ms(t0):.0f}ms）")
    run_server(args.host, args.port, payload, dem_path, open_browser=not args.no_open_browser)


if __name__ == "__main__":
    main()
