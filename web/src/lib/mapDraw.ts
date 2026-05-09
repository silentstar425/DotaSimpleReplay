import type { GuiPayload, HeatmapSettings, HeroTrailSettings, MapViewState, VisionSettings } from "../types/replay";
import {
  applyMapViewTransform,
  computeRecentHeroPoints,
  deathInfoAtTick,
  drawEntityGlyph,
  entityShortName,
  getMapCropRect,
  isVisibleByVision,
  mapFramePad,
  mapToCanvas,
  resizeCanvasToMapAspect,
  shortHeroName,
  stateAtTick,
} from "./replayUtils";

export function renderWorldEntities(
  ctx: CanvasRenderingContext2D,
  tick: number,
  data: GuiPayload,
  canvas: HTMLCanvasElement,
  visionSettings: VisionSettings
): void {
  for (const timeline of data.entity_timelines || []) {
    const st = stateAtTick(timeline, tick);
    if (!st || st.x === null || st.y === null || !st.active) continue;
    if (!isVisibleByVision(st.x, st.y, tick, data, visionSettings)) continue;
    const [cx, cy] = mapToCanvas(st.x, st.y, data.map_bounds, canvas);
    drawEntityGlyph(ctx, cx, cy, timeline);

    if (
      timeline.category === "roshan" ||
      timeline.category === "tormentor" ||
      timeline.category === "lotus_pool"
    ) {
      ctx.fillStyle = "#d9e4f0";
      ctx.font = "10px Arial";
      ctx.textAlign = "left";
      ctx.textBaseline = "bottom";
      ctx.fillText(entityShortName(timeline.entity_name), cx + 10, cy - 2);
    }
  }
}

function renderHeroTrails(
  ctx: CanvasRenderingContext2D,
  tick: number,
  data: GuiPayload,
  canvas: HTMLCanvasElement,
  settings: HeroTrailSettings,
  visionSettings: VisionSettings
): void {
  if (!settings.enabled) return;
  const durationTicks = Math.max(1, Math.round(settings.durationSec * data.tick_rate));
  for (const timeline of data.player_timelines) {
    if (!settings.selectedHeroes.has(timeline.hero_name)) continue;
    const pts = computeRecentHeroPoints(
      timeline,
      tick,
      settings.durationSec,
      settings.sampleEveryTicks,
      data.tick_rate
    );
    for (const pt of pts) {
      if (!isVisibleByVision(pt.x, pt.y, tick, data, visionSettings)) continue;
      const [cx, cy] = mapToCanvas(pt.x, pt.y, data.map_bounds, canvas);
      let alpha = 0.85;
      if (settings.fadeOut) {
        const age = Math.max(0, tick - pt.tick);
        const factor = 1 - age / durationTicks;
        alpha = Math.max(0, Math.min(1, factor)) * 0.95;
      }
      ctx.save();
      ctx.globalAlpha = alpha;
      ctx.beginPath();
      ctx.fillStyle = timeline.team === 2 ? "#63d471" : "#ff7668";
      ctx.arc(cx, cy, Math.max(1, settings.dotRadius), 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }
  }
}

function renderHeroHeatmap(
  ctx: CanvasRenderingContext2D,
  tick: number,
  data: GuiPayload,
  canvas: HTMLCanvasElement,
  heatmapSettings: HeatmapSettings,
  visionSettings: VisionSettings
): void {
  if (!heatmapSettings.enabled) return;
  const intervalTicks = Math.max(1, Math.round(heatmapSettings.intervalSec * data.tick_rate));
  for (const timeline of data.player_timelines) {
    const pts = computeRecentHeroPoints(
      timeline,
      tick,
      heatmapSettings.durationSec,
      intervalTicks,
      data.tick_rate
    );
    for (const pt of pts) {
      if (!isVisibleByVision(pt.x, pt.y, tick, data, visionSettings)) continue;
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
}

export interface DrawMapParams {
  ctx: CanvasRenderingContext2D;
  canvas: HTMLCanvasElement;
  tick: number;
  data: GuiPayload;
  mapBackgroundImage: HTMLImageElement | null;
  mapBackgroundLoaded: boolean;
  heroTrailSettings: HeroTrailSettings;
  heatmapSettings: HeatmapSettings;
  visionSettings: VisionSettings;
  mapView: MapViewState;
}

export function drawMapCanvas(p: DrawMapParams): void {
  const {
    ctx,
    canvas,
    tick,
    data,
    mapBackgroundImage,
    mapBackgroundLoaded,
    heroTrailSettings,
    heatmapSettings,
    visionSettings,
    mapView,
  } = p;

  resizeCanvasToMapAspect(canvas);
  ctx.fillStyle = "#111";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.save();
  applyMapViewTransform(ctx, canvas, mapView);

  if (mapBackgroundLoaded && mapBackgroundImage) {
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

  renderWorldEntities(ctx, tick, data, canvas, visionSettings);
  renderHeroHeatmap(ctx, tick, data, canvas, heatmapSettings, visionSettings);
  renderHeroTrails(ctx, tick, data, canvas, heroTrailSettings, visionSettings);

  for (const timeline of data.player_timelines) {
    const st = stateAtTick(timeline, tick);
    if (!st || st.x === null || st.y === null) continue;
    const death = deathInfoAtTick(timeline, tick);
    if (death.is_dead || st.hp <= 0) continue;
    if (!isVisibleByVision(st.x, st.y, tick, data, visionSettings)) continue;

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
}
