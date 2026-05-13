import type {
  EntityTimeline,
  GuiPayload,
  HeroState,
  MapViewState,
  PlayerTimeline,
  VisionSettings,
} from "../types/replay";

export const mapFramePad = 10;
export const mapCoordPad = 16;
export const mapFrameRatio = 1;

export const mapCropConfig = {
  loadWidth: 1045,
  loadHeight: 1070,
  offsetX: 69,
  offsetY: 65,
};

export function shortHeroName(name: string): string {
  const prefix = "npc_dota_hero_";
  return name.startsWith(prefix) ? name.slice(prefix.length) : name;
}

export function heroAvatarText(name: string): string {
  const s = shortHeroName(name).replaceAll("_", " ");
  const parts = s.split(" ").filter(Boolean);
  if (parts.length === 0) return "H";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

export function formatGameTime(seconds: number): string {
  const sign = seconds < 0 ? "-" : "";
  const absVal = Math.abs(seconds);
  const mm = Math.floor(absVal / 60);
  const ss = absVal - mm * 60;
  return `${sign}${String(mm).padStart(2, "0")}:${ss.toFixed(2).padStart(5, "0")}`;
}

export function mapToCanvas(
  x: number,
  y: number,
  bounds: GuiPayload["map_bounds"],
  canvas: HTMLCanvasElement
): [number, number] {
  const pad = mapCoordPad;
  const nx =
    bounds.max_x === bounds.min_x ? 0 : (x - bounds.min_x) / (bounds.max_x - bounds.min_x);
  const ny =
    bounds.max_y === bounds.min_y ? 0 : (y - bounds.min_y) / (bounds.max_y - bounds.min_y);
  const cx = pad + nx * (canvas.width - 2 * pad);
  const cy = pad + (1 - ny) * (canvas.height - 2 * pad);
  return [cx, cy];
}

export function escapeHtml(s: string | null | undefined): string {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function upperBound(arr: number[], target: number): number {
  let left = 0;
  let right = arr.length;
  while (left < right) {
    const mid = (left + right) >> 1;
    if (arr[mid] <= target) left = mid + 1;
    else right = mid;
  }
  return left;
}

export function stateAtTick<T extends { ticks: number[]; states: unknown[] }>(
  timeline: T,
  tick: number
): T["states"][number] | null {
  const idx = upperBound(timeline.ticks, tick) - 1;
  if (idx < 0) return null;
  return timeline.states[idx] as T["states"][number];
}

export function entityShortName(name: string | null | undefined): string {
  if (!name) return "";
  return String(name)
    .replace("npc_dota_", "")
    .replace("goodguys_", "")
    .replace("badguys_", "")
    .replace("neutral_", "");
}

export function entityGlyph(timeline: EntityTimeline): string {
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
}

export function entityColors(timeline: EntityTimeline): { fill: string; stroke: string } {
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
}

export function drawEntityGlyph(
  ctx2: CanvasRenderingContext2D,
  cx: number,
  cy: number,
  timeline: EntityTimeline
): void {
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
}

export function killsAtTick(timeline: PlayerTimeline, tick: number): number {
  return upperBound(timeline.kill_event_ticks, tick);
}

export function deathInfoAtTick(
  timeline: PlayerTimeline,
  tick: number
): { is_dead: boolean; remaining_ticks: number | null } {
  for (const w of timeline.death_windows) {
    const inRange = tick >= w.start_tick && (w.end_tick === null || tick < w.end_tick);
    if (!inRange) continue;
    return {
      is_dead: true,
      remaining_ticks: w.end_tick === null ? null : Math.max(0, w.end_tick - tick),
    };
  }
  return { is_dead: false, remaining_ticks: 0 };
}

export function defaultHeroState(): HeroState {
  return {
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
  };
}

export function getVisionSourceTimelines(
  data: GuiPayload,
  visionSettings: VisionSettings
): PlayerTimeline[] {
  if (visionSettings.mode === "team1") {
    return data.player_timelines.filter((x) => Number(x.team) === visionSettings.team1);
  }
  if (visionSettings.mode === "team2") {
    return data.player_timelines.filter((x) => Number(x.team) === visionSettings.team2);
  }
  return data.player_timelines;
}

/**
 * 将世界坐标的圆形视野半径换算为画布像素下的椭圆半径。
 * 由于 map_bounds 在 X/Y 方向上不一定等比，世界圆映射到画布上是椭圆。
 */
export function heroVisionEllipseRadiiPx(
  wx: number,
  wy: number,
  worldRadius: number,
  bounds: GuiPayload["map_bounds"],
  canvas: HTMLCanvasElement
): { cx: number; cy: number; rx: number; ry: number } {
  const [cx, cy] = mapToCanvas(wx, wy, bounds, canvas);
  const [cxRx, cyRx] = mapToCanvas(wx + worldRadius, wy, bounds, canvas);
  const [cxRy, cyRy] = mapToCanvas(wx, wy + worldRadius, bounds, canvas);
  const rx = Math.hypot(cxRx - cx, cyRx - cy);
  const ry = Math.hypot(cxRy - cx, cyRy - cy);
  return { cx, cy, rx, ry };
}

export function getMapCropRect(img: HTMLImageElement): {
  sourceX: number;
  sourceY: number;
  cropWidth: number;
  cropHeight: number;
} {
  const cropWidth = Math.max(1, Math.min(Math.round(mapCropConfig.loadWidth), img.width));
  const cropHeight = Math.max(1, Math.min(Math.round(mapCropConfig.loadHeight), img.height));
  const maxOffsetX = Math.max(img.width - cropWidth, 0);
  const maxOffsetY = Math.max(img.height - cropHeight, 0);
  const offsetX = Math.max(0, Math.min(Math.round(mapCropConfig.offsetX), maxOffsetX));
  const offsetYFromBottom = Math.max(0, Math.min(Math.round(mapCropConfig.offsetY), maxOffsetY));
  const sourceX = offsetX;
  const sourceY = img.height - offsetYFromBottom - cropHeight;
  return { sourceX, sourceY, cropWidth, cropHeight };
}

export function resizeCanvasToMapAspect(canvas: HTMLCanvasElement): void {
  const availableHeight = Math.max(window.innerHeight - 144, 1);
  const containerWidth = canvas.parentElement ? canvas.parentElement.clientWidth : 0;
  const availableWidth = Math.max(containerWidth - 20, 1);
  const mapHeightWidthRatio = 1 / mapFrameRatio;
  const targetWidth = Math.min(availableWidth, availableHeight / mapHeightWidthRatio);
  const targetHeight = Math.min(availableHeight, availableWidth * mapHeightWidthRatio);
  const w = Math.max(Math.round(targetWidth), 1);
  const h = Math.max(Math.round(targetHeight), 1);
  canvas.width = w;
  canvas.height = h;
  canvas.style.width = `${w}px`;
  canvas.style.height = `${h}px`;
}

export function applyMapViewTransform(
  ctx: CanvasRenderingContext2D,
  canvas: HTMLCanvasElement,
  mapView: MapViewState
): void {
  const centerX = canvas.width / 2;
  const centerY = canvas.height / 2;
  ctx.translate(centerX + mapView.panX, centerY + mapView.panY);
  ctx.scale(mapView.zoom, mapView.zoom);
  ctx.translate(-centerX, -centerY);
}

export function screenToPreView(
  screenX: number,
  screenY: number,
  canvas: HTMLCanvasElement,
  mapView: MapViewState
): { x: number; y: number } {
  const centerX = canvas.width / 2;
  const centerY = canvas.height / 2;
  return {
    x: (screenX - centerX - mapView.panX) / mapView.zoom + centerX,
    y: (screenY - centerY - mapView.panY) / mapView.zoom + centerY,
  };
}

export function computeRecentHeroPoints(
  timeline: PlayerTimeline,
  tick: number,
  durationSec: number,
  everyTicks: number,
  tickRate: number
): { tick: number; x: number; y: number }[] {
  const out: { tick: number; x: number; y: number }[] = [];
  const startTick = Math.max(0, tick - Math.round(durationSec * tickRate));
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
}
