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

export function pointSegmentDistSq(
  px: number,
  py: number,
  x1: number,
  y1: number,
  x2: number,
  y2: number
): number {
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
}

export function isSightBlockedByTree(
  sx: number,
  sy: number,
  tx: number,
  ty: number,
  visionSettings: VisionSettings
): boolean {
  const radiusSq = visionSettings.treeRadius * visionSettings.treeRadius;
  for (const tree of visionSettings.treeBlockers) {
    if (pointSegmentDistSq(tree.x, tree.y, sx, sy, tx, ty) <= radiusSq) {
      return true;
    }
  }
  return false;
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

export function isVisibleByVision(
  x: number,
  y: number,
  tick: number,
  data: GuiPayload,
  visionSettings: VisionSettings
): boolean {
  if (!visionSettings.enabled) return true;
  const sources = getVisionSourceTimelines(data, visionSettings);
  const radiusSq = visionSettings.heroVisionRadius * visionSettings.heroVisionRadius;
  for (const source of sources) {
    const st = stateAtTick(source, tick) as HeroState | null;
    if (!st || st.x === null || st.y === null || st.hp <= 0) continue;
    const death = deathInfoAtTick(source, tick);
    if (death.is_dead) continue;
    const dx = x - st.x;
    const dy = y - st.y;
    if (dx * dx + dy * dy > radiusSq) continue;
    if (!isSightBlockedByTree(st.x, st.y, x, y, visionSettings)) return true;
  }
  return false;
}

export function initTreeBlockers(data: GuiPayload): { x: number; y: number }[] {
  const trees: { x: number; y: number }[] = [];
  const spanX = data.map_bounds.max_x - data.map_bounds.min_x;
  const spanY = data.map_bounds.max_y - data.map_bounds.min_y;
  const cols = 28;
  const rows = 28;
  for (let r = 2; r < rows - 2; r += 1) {
    for (let c = 2; c < cols - 2; c += 1) {
      const seed = (r * 73856093) ^ (c * 19349663);
      if (seed % 100 > 22) continue;
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
  return trees;
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
