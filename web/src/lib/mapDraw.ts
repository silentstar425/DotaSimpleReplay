import type {
  GuiPayload,
  HeatmapSettings,
  HeroTrailSettings,
  MapViewState,
  VisionSettings,
} from "../types/replay";
import {
  applyMapViewTransform,
  computeRecentHeroPoints,
  deathInfoAtTick,
  drawEntityGlyph,
  entityShortName,
  getMapCropRect,
  getVisionSourceTimelines,
  heroVisionEllipseRadiiPx,
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
  canvas: HTMLCanvasElement
): void {
  for (const timeline of data.entity_timelines || []) {
    const st = stateAtTick(timeline, tick);
    if (!st || st.x === null || st.y === null || !st.active) continue;
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
  settings: HeroTrailSettings
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
  heatmapSettings: HeatmapSettings
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

/**
 * 仅承载迷雾 alpha 的离屏画布。
 *
 * 直接在主画布上用 destination-out 会把地图一起抠成透明洞；
 * 改为在离屏画布上画雾再 drawImage 叠回，可以保留地图底色。
 */
let fogScratchCanvas: HTMLCanvasElement | null = null;

function renderVisionFogOfWar(
  ctx: CanvasRenderingContext2D,
  tick: number,
  data: GuiPayload,
  canvas: HTMLCanvasElement,
  visionSettings: VisionSettings,
  mapView: MapViewState
): void {
  if (!visionSettings.enabled) return;
  const mx0 = mapFramePad;
  const my0 = mapFramePad;
  const mw = Math.max(0, canvas.width - 2 * mapFramePad);
  const mh = Math.max(0, canvas.height - 2 * mapFramePad);
  if (mw <= 0 || mh <= 0) return;

  if (
    !fogScratchCanvas ||
    fogScratchCanvas.width !== canvas.width ||
    fogScratchCanvas.height !== canvas.height
  ) {
    fogScratchCanvas = document.createElement("canvas");
    fogScratchCanvas.width = canvas.width;
    fogScratchCanvas.height = canvas.height;
  }
  const fctx = fogScratchCanvas.getContext("2d");
  if (!fctx) return;

  fctx.setTransform(1, 0, 0, 1, 0, 0);
  fctx.clearRect(0, 0, canvas.width, canvas.height);
  fctx.globalAlpha = 1;
  fctx.globalCompositeOperation = "source-over";
  fctx.save();
  applyMapViewTransform(fctx, canvas, mapView);

  fctx.beginPath();
  fctx.rect(mx0, my0, mw, mh);
  fctx.clip();

  const fogA = Math.max(0, Math.min(1, visionSettings.fogOpacity));
  fctx.fillStyle = `rgba(0, 0, 0, ${fogA})`;
  fctx.fillRect(mx0, my0, mw, mh);

  fctx.globalCompositeOperation = "destination-out";
  fctx.fillStyle = "#ffffff";
  const sources = getVisionSourceTimelines(data, visionSettings);
  for (const source of sources) {
    const st = stateAtTick(source, tick);
    if (!st || st.x === null || st.y === null || st.hp <= 0) continue;
    if (deathInfoAtTick(source, tick).is_dead) continue;
    const { cx, cy, rx, ry } = heroVisionEllipseRadiiPx(
      st.x,
      st.y,
      visionSettings.heroVisionRadius,
      data.map_bounds,
      canvas
    );
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

  renderWorldEntities(ctx, tick, data, canvas);
  renderHeroHeatmap(ctx, tick, data, canvas, heatmapSettings);
  renderHeroTrails(ctx, tick, data, canvas, heroTrailSettings);

  for (const timeline of data.player_timelines) {
    const st = stateAtTick(timeline, tick);
    if (!st || st.x === null || st.y === null) continue;
    const death = deathInfoAtTick(timeline, tick);
    if (death.is_dead || st.hp <= 0) continue;

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

  renderVisionFogOfWar(ctx, tick, data, canvas, visionSettings, mapView);

  ctx.restore();
}
