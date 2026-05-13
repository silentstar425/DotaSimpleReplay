import { useCallback, useEffect, useRef, useState } from "react";
import "./App.css";
import { drawMapCanvas } from "./lib/mapDraw";
import {
  deathInfoAtTick,
  defaultHeroState,
  formatGameTime,
  heroAvatarText,
  killsAtTick,
  shortHeroName,
  stateAtTick,
} from "./lib/replayUtils";
import type {
  GuiPayload,
  HeatmapSettings,
  HeroTrailSettings,
  MapViewState,
  ReplayRecord,
  VisionSettings,
} from "./types/replay";

function formatDownloadTime(raw: string | null | undefined): string {
  if (!raw) return "-";
  try {
    return new Date(raw).toLocaleString("zh-CN", { hour12: false });
  } catch {
    return raw;
  }
}

function statusClass(parsed: boolean, parseError: string): string {
  if (parsed) return "replay-status-ok";
  if (parseError) return "replay-status-bad";
  return "replay-status-pending";
}

function statusText(parsed: boolean, parseError: string): string {
  if (parsed) return "已解析";
  if (parseError) return "解析失败";
  return "未解析";
}

export default function App() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const mapImgRef = useRef<HTMLImageElement | null>(null);
  const [mapBgLoaded, setMapBgLoaded] = useState(false);

  const [data, setData] = useState<GuiPayload | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [titleText, setTitleText] = useState("Dota2 回放可视化");

  const [playing, setPlaying] = useState(false);
  const [currentTickFloat, setCurrentTickFloat] = useState(0);
  const playbackAnchorRealMsRef = useRef(0);
  const playbackAnchorTickRef = useRef(0);
  const [playbackSpeed, setPlaybackSpeed] = useState(1);

  const [fpsInput, setFpsInput] = useState(30);

  const [boardMetric, setBoardMetric] = useState<
    "net_worth" | "kda" | "lh_dn" | "level"
  >("net_worth");

  const [heroTrailSettings, setHeroTrailSettings] = useState<HeroTrailSettings>(() => ({
    enabled: false,
    sampleEveryTicks: 12,
    dotRadius: 2.0,
    durationSec: 30,
    fadeOut: true,
    selectedHeroes: new Set<string>(),
  }));

  const [heatmapSettings, setHeatmapSettings] = useState<HeatmapSettings>(() => ({
    enabled: false,
    intervalSec: 2.0,
    radius: 36,
    opacity: 0.18,
    durationSec: 60,
  }));

  const [visionSettings, setVisionSettings] = useState<VisionSettings>(() => ({
    enabled: false,
    mode: "both",
    heroVisionRadius: 1600,
    fogOpacity: 0.42,
    team1: 2,
    team2: 3,
  }));

  const [mapView, setMapView] = useState<MapViewState>(() => ({
    zoom: 1.0,
    minZoom: 1.0,
    maxZoom: 4.0,
    panX: 0.0,
    panY: 0.0,
  }));

  const mapDraggingRef = useRef(false);
  const mapLastRef = useRef({ x: 0, y: 0 });
  const [viewportTick, setViewportTick] = useState(0);

  const [trailModalOpen, setTrailModalOpen] = useState(false);
  const [heatmapModalOpen, setHeatmapModalOpen] = useState(false);
  const [replaySelectOpen, setReplaySelectOpen] = useState(false);
  const [replayDownloadOpen, setReplayDownloadOpen] = useState(false);

  const [replayListCache, setReplayListCache] = useState<ReplayRecord[]>([]);
  const [replaySelectError, setReplaySelectError] = useState("");
  const [replayIdInput, setReplayIdInput] = useState("");
  const [replayDownloadResult, setReplayDownloadResult] = useState("");
  const [autoParseAfterDownload, setAutoParseAfterDownload] = useState(true);
  const [downloadInFlight, setDownloadInFlight] = useState(false);

  const hydrateFromPayload = useCallback(
    (payload: GuiPayload, titleFromMatch: boolean) => {
      setData(payload);
      if (titleFromMatch) {
        setTitleText(`Dota2 回放可视化（Match ${payload.match_id}）`);
      } else {
        setTitleText("Dota2 回放可视化");
      }
      setCurrentTickFloat(0);
      const teams = [...new Set(payload.player_timelines.map((x) => Number(x.team)))].sort(
        (a, b) => a - b
      );
      setVisionSettings((v) => ({
        ...v,
        mode: "both",
        team1: teams[0] ?? 2,
        team2: teams[1] ?? teams[0] ?? 3,
      }));
      const heroSet = new Set(payload.player_timelines.map((t) => t.hero_name));
      setHeroTrailSettings((h) => ({ ...h, selectedHeroes: heroSet }));
      setFpsInput(payload.playback_fps || 30);
      setPlaying(false);
    },
    []
  );

  useEffect(() => {
    const onResize = () => setViewportTick((x) => x + 1);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  useEffect(() => {
    const onUp = () => {
      mapDraggingRef.current = false;
      if (canvasRef.current) canvasRef.current.style.cursor = "grab";
    };
    window.addEventListener("mouseup", onUp);
    return () => window.removeEventListener("mouseup", onUp);
  }, []);

  useEffect(() => {
    const img = new Image();
    img.onload = () => {
      mapImgRef.current = img;
      setMapBgLoaded(true);
    };
    img.onerror = () => {
      mapImgRef.current = null;
      setMapBgLoaded(false);
    };
    img.src = "/assets/maps/map_full.png";
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/data");
        const d = (await res.json()) as GuiPayload;
        if (cancelled) return;
        hydrateFromPayload(d, false);
        setLoadError(null);
      } catch (e) {
        if (!cancelled) {
          setLoadError(`无法加载回放数据：${String(e)}`);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [hydrateFromPayload]);

  const currentTickInt = data
    ? Math.max(0, Math.min(data.game_end_tick, Math.round(currentTickFloat)))
    : 0;

  useEffect(() => {
    if (!data || !canvasRef.current) return;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    drawMapCanvas({
      ctx,
      canvas,
      tick: currentTickInt,
      data,
      mapBackgroundImage: mapImgRef.current,
      mapBackgroundLoaded: mapBgLoaded,
      heroTrailSettings,
      heatmapSettings,
      visionSettings,
      mapView,
    });
  }, [
    data,
    currentTickInt,
    mapBgLoaded,
    heroTrailSettings,
    heatmapSettings,
    visionSettings,
    mapView,
    viewportTick,
  ]);

  useEffect(() => {
    if (!playing || !data) return;
    const fps = Math.max(1, Math.min(240, fpsInput || data.playback_fps || 30));
    const delay = Math.max(Math.round(1000 / fps), 1);
    const id = window.setInterval(() => {
      const elapsedSec = (performance.now() - playbackAnchorRealMsRef.current) / 1000;
      const target =
        playbackAnchorTickRef.current +
        elapsedSec * data.tick_rate * playbackSpeed;
      if (target >= data.game_end_tick) {
        setCurrentTickFloat(data.game_end_tick);
        setPlaying(false);
        return;
      }
      setCurrentTickFloat(target);
    }, delay);
    return () => window.clearInterval(id);
  }, [playing, data, fpsInput, playbackSpeed]);

  const stopPlayback = useCallback(() => {
    setPlaying(false);
  }, []);

  const startPlayback = useCallback(() => {
    if (!data) return;
    playbackAnchorRealMsRef.current = performance.now();
    playbackAnchorTickRef.current = currentTickFloat;
    setPlaying(true);
  }, [data, currentTickFloat]);

  const renderFromFloat = useCallback(
    (tickFloat: number) => {
      if (!data) return;
      const clamped = Math.max(0, Math.min(data.game_end_tick, tickFloat));
      setCurrentTickFloat(clamped);
    },
    [data]
  );

  const seekBySeconds = useCallback(
    (deltaSec: number) => {
      if (!data) return;
      const deltaTick = deltaSec * data.tick_rate;
      const next = Math.max(
        0,
        Math.min(data.game_end_tick, currentTickFloat + deltaTick)
      );
      setCurrentTickFloat(next);
      if (playing) {
        playbackAnchorRealMsRef.current = performance.now();
        playbackAnchorTickRef.current = next;
      }
    },
    [data, currentTickFloat, playing]
  );

  const loadReplayList = useCallback(async () => {
    setReplaySelectError("");
    try {
      const res = await fetch("/replays");
      const obj = (await res.json()) as { replays?: ReplayRecord[] };
      if (!res.ok || !obj || !Array.isArray(obj.replays)) {
        throw new Error(`HTTP ${res.status}`);
      }
      setReplayListCache(obj.replays);
    } catch (err) {
      setReplayListCache([]);
      setReplaySelectError(`加载录像列表失败：${String(err)}`);
    }
  }, []);

  const loadReplayRecord = async (record: ReplayRecord) => {
    if (!record?.dem_path) return;
    try {
      const res = await fetch("/load_replay", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dem_path: record.dem_path }),
      });
      const obj = (await res.json()) as { ok?: boolean; error?: string; payload?: GuiPayload };
      if (!res.ok || !obj?.ok || !obj.payload) {
        throw new Error(obj?.error || `HTTP ${res.status}`);
      }
      hydrateFromPayload(obj.payload, true);
      setReplaySelectOpen(false);
    } catch (err) {
      alert(`加载录像失败：${String(err)}`);
    }
  };

  const parseReplay = async (record: ReplayRecord) => {
    if (!record?.dem_path) return;
    try {
      const res = await fetch("/parse_replay", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dem_path: record.dem_path }),
      });
      const obj = (await res.json()) as { ok?: boolean; error?: string };
      if (!res.ok || !obj?.ok) {
        throw new Error(obj?.error || `HTTP ${res.status}`);
      }
      await loadReplayList();
    } catch (err) {
      alert(`解析失败：${String(err)}`);
    }
  };

  const clearCache = async () => {
    if (!data) return;
    const ok = window.confirm("确定要删除当前录像的缓存文件吗？该操作不可撤销。");
    if (!ok) return;
    const ok2 = window.confirm("请再次确认：删除后下次将重新解析录像，可能较慢。是否继续？");
    if (!ok2) return;
    try {
      const res = await fetch("/clear_cache", { method: "POST" });
      const obj = (await res.json()) as { deleted?: boolean; cache_path?: string };
      if (obj?.deleted) {
        setData((d) => (d ? { ...d, cache_hit: false } : d));
        alert(`缓存已删除：${obj.cache_path ?? ""}`);
      } else {
        alert(`未删除缓存（可能不存在）：${obj?.cache_path ?? "unknown"}`);
      }
    } catch (err) {
      alert(`清理缓存失败：${String(err)}`);
    }
  };

  const isDuplicateMatchId = useCallback(
    (rid: string): boolean => {
      const ridNum = Number(rid);
      if (!Number.isFinite(ridNum) || ridNum <= 0) return false;
      for (const r of replayListCache) {
        if (String(r.replay_id || "").trim() === rid) return true;
        const fromName = r.file_name?.match(/(\d{6,})/)?.[1];
        if (fromName && fromName === rid) return true;
      }
      return false;
    },
    [replayListCache]
  );

  const downloadReplayById = async () => {
    if (downloadInFlight) return;
    const replayId = replayIdInput.trim();
    if (!replayId) {
      setReplayDownloadResult("请输入录像ID。");
      return;
    }
    if (!/^[0-9]{6,}$/.test(replayId)) {
      setReplayDownloadResult("请输入合法的数字录像ID（至少 6 位）。");
      return;
    }
    if (isDuplicateMatchId(replayId)) {
      setReplayDownloadResult(`本机已存在录像 ${replayId}，无需重复下载。`);
      return;
    }
    setReplayDownloadResult("正在下载，请稍候...");
    setDownloadInFlight(true);
    try {
      const res = await fetch("/download_replay_by_id", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ replay_id: replayId }),
      });
      const obj = (await res.json()) as { ok?: boolean; error?: string; file_path?: string };
      if (!res.ok || !obj?.ok) {
        throw new Error(obj?.error || `HTTP ${res.status}`);
      }
      setReplayDownloadResult(`下载成功：${obj.file_path ?? ""}`);
      await loadReplayList();

      if (autoParseAfterDownload && obj.file_path) {
        setReplayDownloadResult(`下载成功，正在解析：${obj.file_path}`);
        try {
          const parseRes = await fetch("/parse_replay", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ dem_path: obj.file_path }),
          });
          const parseObj = (await parseRes.json()) as {
            ok?: boolean;
            error?: string;
          };
          if (!parseRes.ok || !parseObj?.ok) {
            throw new Error(parseObj?.error || `HTTP ${parseRes.status}`);
          }
          setReplayDownloadResult(`下载并解析完成：${obj.file_path}`);
          await loadReplayList();
        } catch (parseErr) {
          setReplayDownloadResult(
            `下载成功，但自动解析失败：${String(parseErr)}`
          );
        }
      }
    } catch (err) {
      setReplayDownloadResult(`下载失败：${String(err)}`);
    } finally {
      setDownloadInFlight(false);
    }
  };

  const boardRows = (() => {
    if (!data) return [];
    const rows: {
      playerId: number;
      name: string;
      hero: string;
      valueText: string;
      sortValue: number;
    }[] = [];
    for (const timeline of data.player_timelines) {
      const st = (stateAtTick(timeline, currentTickInt) as ReturnType<
        typeof defaultHeroState
      > | null) || defaultHeroState();
      const kills = killsAtTick(timeline, currentTickInt);
      const deaths = st.total_deaths;
      const assists = timeline.final_kda.assists;
      let sortValue = 0;
      let valueText = "";
      if (boardMetric === "net_worth") {
        sortValue = st.net_worth || 0;
        valueText = `${sortValue}`;
      } else if (boardMetric === "kda") {
        sortValue = kills * 100000 - deaths * 100 + assists;
        valueText = `${kills}/${deaths}/${assists}`;
      } else if (boardMetric === "lh_dn") {
        sortValue = (st.lh || 0) * 1000 + (st.dn || 0);
        valueText = `${st.lh || 0}/${st.dn || 0}`;
      } else {
        sortValue = st.level || 0;
        valueText = `${sortValue}`;
      }
      rows.push({
        playerId: timeline.player_id,
        name: timeline.player_name || shortHeroName(timeline.hero_name),
        hero: shortHeroName(timeline.hero_name),
        valueText,
        sortValue,
      });
    }
    rows.sort(
      (a, b) => b.sortValue - a.sortValue || a.hero.localeCompare(b.hero)
    );
    return rows;
  })();

  const sortedStatusTimelines = data
    ? [...data.player_timelines].sort((a, b) => {
        if (a.team !== b.team) return a.team - b.team;
        return a.player_id - b.player_id;
      })
    : [];

  const tickLine =
    data &&
    `Tick: ${currentTickInt} | 游戏时间: ${formatGameTime(
      (currentTickInt - data.game_start_tick) / data.tick_rate
    )} | 游戏开始 tick: ${data.game_start_tick}`;

  const visionOptions = (() => {
    const t1 = visionSettings.team1;
    const t2 = visionSettings.team2;
    return (
      <>
        <option value="both">双方视野</option>
        <option value="team1">{`阵营1视野(T${t1})`}</option>
        <option value="team2">{`阵营2视野(T${t2})`}</option>
      </>
    );
  })();

  if (loadError && !data) {
    return <div className="app-error">{loadError}</div>;
  }

  if (!data) {
    return <div className="app-error">正在加载…</div>;
  }

  return (
    <>
      <div className="app">
        <aside className="side left">
          <h3>玩家看板</h3>
          <label className="small-muted" htmlFor="boardMetric">
            排序指标
          </label>
          <select
            id="boardMetric"
            value={boardMetric}
            onChange={(e) =>
              setBoardMetric(e.target.value as typeof boardMetric)
            }
          >
            <option value="net_worth">资产总额</option>
            <option value="kda">K/D/A</option>
            <option value="lh_dn">正补/反补</option>
            <option value="level">等级</option>
          </select>
          <div style={{ height: 10 }} />
          <div id="boardList" className="scroll">
            {boardRows.map((row) => (
              <div className="board-row" key={row.playerId}>
                <div className="name">
                  {row.hero}
                  <div className="small-muted">({row.name})</div>
                </div>
                <div className="val">{row.valueText}</div>
              </div>
            ))}
          </div>
        </aside>

        <main className="center">
          <div className="meta" id="titleLine">
            {titleText}
          </div>
          <div className="meta" id="tickLine">
            {tickLine}
          </div>
          <div className="controls">
            <div className="control-row">
              <button
                type="button"
                style={{ background: "#3a4d63" }}
                onClick={async () => {
                  setReplaySelectOpen(true);
                  await loadReplayList();
                }}
              >
                录像选择
              </button>
              <button
                type="button"
                style={{ background: "#3a4d63" }}
                onClick={() => {
                  setReplayDownloadResult("");
                  setReplayDownloadOpen(true);
                }}
              >
                按ID下载录像
              </button>
              <button
                type="button"
                style={{ background: "#8b1e2d" }}
                onClick={clearCache}
              >
                清理缓存
              </button>
              <button
                type="button"
                style={{ background: "#3a4d63" }}
                onClick={() =>
                  setHeroTrailSettings((h) => ({ ...h, enabled: !h.enabled }))
                }
              >
                轨迹：{heroTrailSettings.enabled ? "开" : "关"}
              </button>
              <button
                type="button"
                style={{ background: "#3a4d63" }}
                onClick={() => setTrailModalOpen(true)}
              >
                轨迹设置
              </button>
              <button
                type="button"
                style={{ background: "#3a4d63" }}
                onClick={() =>
                  setHeatmapSettings((h) => ({ ...h, enabled: !h.enabled }))
                }
              >
                热力图：{heatmapSettings.enabled ? "开" : "关"}
              </button>
              <button
                type="button"
                style={{ background: "#3a4d63" }}
                onClick={() => setHeatmapModalOpen(true)}
              >
                热力图设置
              </button>
              <button
                type="button"
                style={{ background: "#3a4d63" }}
                onClick={() =>
                  setVisionSettings((v) => ({ ...v, enabled: !v.enabled }))
                }
              >
                视野：{visionSettings.enabled ? "开" : "关"}
              </button>
              <select
                id="visionTeamSelect"
                value={visionSettings.mode}
                disabled={!visionSettings.enabled}
                onChange={(e) =>
                  setVisionSettings((v) => ({
                    ...v,
                    mode: e.target.value as VisionSettings["mode"],
                  }))
                }
              >
                {visionOptions}
              </select>
            </div>
            <div className="control-row">
              <button
                type="button"
                onClick={() => {
                  if (playing) stopPlayback();
                  else startPlayback();
                }}
              >
                {playing ? "暂停" : "播放"}
              </button>
              <button
                type="button"
                style={{ background: "#3a4d63" }}
                onClick={() => seekBySeconds(-10)}
              >
                后退10秒
              </button>
              <button
                type="button"
                style={{ background: "#3a4d63" }}
                onClick={() => seekBySeconds(10)}
              >
                前进10秒
              </button>
              <button
                type="button"
                style={{
                  background: playbackSpeed === 0.5 ? "#2d6cdf" : "#3a4d63",
                }}
                onClick={() => {
                  const sp = 0.5;
                  setPlaybackSpeed(sp);
                  if (playing) {
                    playbackAnchorRealMsRef.current = performance.now();
                    playbackAnchorTickRef.current = currentTickFloat;
                  }
                }}
              >
                0.5x
              </button>
              <button
                type="button"
                style={{
                  background: playbackSpeed === 2.0 ? "#2d6cdf" : "#3a4d63",
                }}
                onClick={() => {
                  const sp = 2.0;
                  setPlaybackSpeed(sp);
                  if (playing) {
                    playbackAnchorRealMsRef.current = performance.now();
                    playbackAnchorTickRef.current = currentTickFloat;
                  }
                }}
              >
                2x
              </button>
              <span className="speed-indicator">{playbackSpeed}x</span>
              <div className="slider-wrap">
                <input
                  id="slider"
                  type="range"
                  min={0}
                  max={data.game_end_tick}
                  step={1}
                  value={currentTickInt}
                  onChange={(e) => {
                    const v = Number(e.target.value);
                    renderFromFloat(v);
                    if (playing) {
                      playbackAnchorRealMsRef.current = performance.now();
                      playbackAnchorTickRef.current = v;
                    }
                  }}
                />
                <label htmlFor="fpsInput" className="small-muted">
                  刷新率(FPS)
                </label>
                <input
                  id="fpsInput"
                  type="number"
                  min={1}
                  max={240}
                  step={1}
                  value={fpsInput}
                  onChange={(e) => {
                    const fps = Math.max(
                      1,
                      Math.min(
                        240,
                        Number(e.target.value) || data.playback_fps || 30
                      )
                    );
                    setFpsInput(Math.round(fps));
                    if (playing) {
                      stopPlayback();
                      startPlayback();
                    }
                  }}
                  style={{ width: 76 }}
                />
              </div>
            </div>
          </div>
          <canvas
            id="mapCanvas"
            ref={canvasRef}
            width={1200}
            height={780}
            style={{ cursor: "grab" }}
            onMouseDown={(e) => {
              mapDraggingRef.current = true;
              mapLastRef.current = { x: e.clientX, y: e.clientY };
              e.currentTarget.style.cursor = "grabbing";
            }}
            onMouseLeave={() => {
              mapDraggingRef.current = false;
              if (canvasRef.current) canvasRef.current.style.cursor = "grab";
            }}
            onMouseMove={(e) => {
              if (!mapDraggingRef.current) return;
              const dx = e.clientX - mapLastRef.current.x;
              const dy = e.clientY - mapLastRef.current.y;
              mapLastRef.current = { x: e.clientX, y: e.clientY };
              setMapView((mv) => ({
                ...mv,
                panX: mv.panX + dx,
                panY: mv.panY + dy,
              }));
            }}
            onWheel={(e) => {
              e.preventDefault();
              if (!canvasRef.current) return;
              const rect = canvasRef.current.getBoundingClientRect();
              const sx = e.clientX - rect.left;
              const sy = e.clientY - rect.top;
              const canvas = canvasRef.current;
              const centerX = canvas.width / 2;
              const centerY = canvas.height / 2;
              setMapView((mv) => {
                const preX = (sx - centerX - mv.panX) / mv.zoom + centerX;
                const preY = (sy - centerY - mv.panY) / mv.zoom + centerY;
                const delta = e.deltaY < 0 ? 1.1 : 0.9;
                const newZoom = Math.max(
                  mv.minZoom,
                  Math.min(mv.maxZoom, mv.zoom * delta)
                );
                const newPanX =
                  sx - (preX - centerX) * newZoom - centerX;
                const newPanY =
                  sy - (preY - centerY) * newZoom - centerY;
                return { ...mv, zoom: newZoom, panX: newPanX, panY: newPanY };
              });
            }}
          />
          <div className="legend">
            英雄：绿/红圆点（天辉/夜魇，死亡不显示） | 建筑：基/塔/营/建 | 单位：近/远/车/野
            | 资源点：莲/肉/折
          </div>
        </main>

        <aside className="side right">
          <h3>英雄状态</h3>
          <div className="small-muted" style={{ marginBottom: 8 }}>
            显示：HP / MP / 复活倒计时（死亡时）
          </div>
          <div id="statusList" className="scroll">
            {sortedStatusTimelines.map((timeline) => {
              const st =
                (stateAtTick(timeline, currentTickInt) as ReturnType<
                  typeof defaultHeroState
                > | null) || defaultHeroState();
              const death = deathInfoAtTick(timeline, currentTickInt);
              const hpText = `${Math.max(0, Math.round(st.hp || 0))}/${Math.max(
                0,
                Math.round(st.max_hp || 0)
              )}`;
              const manaText = `${Math.max(0, Math.round(st.mana || 0))}/${Math.max(
                0,
                Math.round(st.max_mana || 0)
              )}`;
              const respawnSec =
                death.remaining_ticks === null
                  ? "?"
                  : (death.remaining_ticks / data.tick_rate).toFixed(1);
              return (
                <div className="status-row" key={timeline.player_id}>
                  <div className="avatar">
                    {heroAvatarText(timeline.hero_name)}
                    {death.is_dead && (
                      <span className="respawn-badge">{respawnSec}s</span>
                    )}
                  </div>
                  <div>
                    <div>
                      <strong>{shortHeroName(timeline.hero_name)}</strong>{" "}
                      <span className="small-muted">
                        (
                        {timeline.player_name ||
                          shortHeroName(timeline.hero_name)}
                        )
                      </span>
                    </div>
                    <div className="hp">HP: {hpText}</div>
                    <div className="mp">MP: {manaText}</div>
                  </div>
                </div>
              );
            })}
          </div>
        </aside>
      </div>

      <div
        id="trailSettingsModal"
        className={`settings-modal${trailModalOpen ? " open" : ""}`}
        onClick={(e) => {
          if (e.target === e.currentTarget) setTrailModalOpen(false);
        }}
      >
        <div className="settings-body">
          <div className="settings-title">
            <span>轨迹设置</span>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setTrailModalOpen(false)}
            >
              关闭
            </button>
          </div>
          <div className="settings-grid">
            <label htmlFor="trailDensityInput">轨迹密度（每几帧显示一个点）</label>
            <input
              id="trailDensityInput"
              type="number"
              min={1}
              max={300}
              step={1}
              value={heroTrailSettings.sampleEveryTicks}
              onChange={(e) => {
                const v = Math.max(
                  1,
                  Math.min(300, Number(e.target.value) || 12)
                );
                setHeroTrailSettings((h) => ({ ...h, sampleEveryTicks: Math.round(v) }));
              }}
            />
            <label htmlFor="trailDotSizeInput">轨迹大小（圆点半径）</label>
            <input
              id="trailDotSizeInput"
              type="number"
              min={1}
              max={20}
              step={0.5}
              value={heroTrailSettings.dotRadius}
              onChange={(e) => {
                const v = Math.max(1, Math.min(20, Number(e.target.value) || 2));
                setHeroTrailSettings((h) => ({ ...h, dotRadius: v }));
              }}
            />
            <label htmlFor="trailLengthSecInput">轨迹长度（最近多少秒）</label>
            <input
              id="trailLengthSecInput"
              type="number"
              min={1}
              max={300}
              step={1}
              value={heroTrailSettings.durationSec}
              onChange={(e) => {
                const v = Math.max(
                  1,
                  Math.min(300, Number(e.target.value) || 30)
                );
                setHeroTrailSettings((h) => ({ ...h, durationSec: Math.round(v) }));
              }}
            />
          </div>
          <label className="settings-hero-item" style={{ marginBottom: 8 }}>
            <input
              id="trailFadeEnabledInput"
              type="checkbox"
              checked={heroTrailSettings.fadeOut}
              onChange={(e) =>
                setHeroTrailSettings((h) => ({ ...h, fadeOut: e.target.checked }))
              }
            />
            <span>轨迹淡出（旧点逐渐透明到 0）</span>
          </label>
          <div className="small-muted" style={{ marginBottom: 6 }}>
            英雄筛选（显示哪些英雄轨迹）
          </div>
          <div id="trailHeroFilterList" className="settings-hero-list">
            {data.player_timelines.map((timeline) => (
              <label className="settings-hero-item" key={timeline.hero_name}>
                <input
                  type="checkbox"
                  checked={heroTrailSettings.selectedHeroes.has(timeline.hero_name)}
                  onChange={(e) => {
                    setHeroTrailSettings((h) => {
                      const next = new Set(h.selectedHeroes);
                      if (e.target.checked) next.add(timeline.hero_name);
                      else next.delete(timeline.hero_name);
                      return { ...h, selectedHeroes: next };
                    });
                  }}
                />
                <span>
                  {shortHeroName(timeline.hero_name)} (
                  {timeline.player_name || shortHeroName(timeline.hero_name)})
                </span>
              </label>
            ))}
          </div>
          <div className="settings-actions">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => {
                setHeroTrailSettings((h) => ({
                  ...h,
                  selectedHeroes: new Set(
                    data.player_timelines.map((x) => x.hero_name)
                  ),
                }));
              }}
            >
              全选
            </button>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => {
                setHeroTrailSettings((h) => ({
                  ...h,
                  selectedHeroes: new Set(),
                }));
              }}
            >
              全不选
            </button>
          </div>
        </div>
      </div>

      <div
        id="heatmapSettingsModal"
        className={`settings-modal${heatmapModalOpen ? " open" : ""}`}
        onClick={(e) => {
          if (e.target === e.currentTarget) setHeatmapModalOpen(false);
        }}
      >
        <div className="settings-body">
          <div className="settings-title">
            <span>热力图设置</span>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setHeatmapModalOpen(false)}
            >
              关闭
            </button>
          </div>
          <div className="settings-grid">
            <label htmlFor="heatmapIntervalSecInput">画圆间隔（秒）</label>
            <input
              id="heatmapIntervalSecInput"
              type="number"
              min={0.1}
              max={60}
              step={0.1}
              value={heatmapSettings.intervalSec}
              onChange={(e) => {
                const v = Math.max(
                  0.1,
                  Math.min(60, Number(e.target.value) || 2)
                );
                setHeatmapSettings((h) => ({ ...h, intervalSec: v }));
              }}
            />
            <label htmlFor="heatmapRadiusInput">圆大小（半径）</label>
            <input
              id="heatmapRadiusInput"
              type="number"
              min={4}
              max={200}
              step={1}
              value={heatmapSettings.radius}
              onChange={(e) => {
                const v = Math.max(4, Math.min(200, Number(e.target.value) || 36));
                setHeatmapSettings((h) => ({ ...h, radius: Math.round(v) }));
              }}
            />
            <label htmlFor="heatmapOpacityInput">不透明度（0~1）</label>
            <input
              id="heatmapOpacityInput"
              type="number"
              min={0.01}
              max={1}
              step={0.01}
              value={heatmapSettings.opacity}
              onChange={(e) => {
                const v = Math.max(
                  0.01,
                  Math.min(1, Number(e.target.value) || 0.18)
                );
                setHeatmapSettings((h) => ({ ...h, opacity: v }));
              }}
            />
            <label htmlFor="heatmapWindowSecInput">时间范围（最近多少秒）</label>
            <input
              id="heatmapWindowSecInput"
              type="number"
              min={1}
              max={300}
              step={1}
              value={heatmapSettings.durationSec}
              onChange={(e) => {
                const v = Math.max(
                  1,
                  Math.min(300, Number(e.target.value) || 60)
                );
                setHeatmapSettings((h) => ({ ...h, durationSec: Math.round(v) }));
              }}
            />
          </div>
        </div>
      </div>

      <div
        id="replaySelectModal"
        className={`settings-modal${replaySelectOpen ? " open" : ""}`}
        onClick={(e) => {
          if (e.target === e.currentTarget) setReplaySelectOpen(false);
        }}
      >
        <div className="settings-body" style={{ width: "min(980px, 95vw)" }}>
          <div className="settings-title">
            <span>录像选择</span>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setReplaySelectOpen(false)}
            >
              关闭
            </button>
          </div>
          <div className="muted-line">
            列表包含录像编号、下载时间、解析状态、解析按钮和播放按钮（仅可播放已解析录像）。
          </div>
          {replaySelectError && (
            <div className="muted-line" style={{ color: "#ff9a9a" }}>
              {replaySelectError}
            </div>
          )}
          <div className="replay-table-wrap">
            <table className="replay-table">
              <thead>
                <tr>
                  <th>录像编号</th>
                  <th>下载时间</th>
                  <th>解析状态</th>
                  <th>解析按钮</th>
                  <th>播放按钮</th>
                </tr>
              </thead>
              <tbody>
                {replayListCache.map((record, idx) => {
                  const parsed = Boolean(record.parsed);
                  const sc = statusClass(parsed, record.parse_error);
                  const st = statusText(parsed, record.parse_error);
                  return (
                    <tr key={`${record.dem_path}-${idx}`}>
                      <td>{record.replay_id || `#${idx + 1}`}</td>
                      <td>{formatDownloadTime(record.downloaded_at)}</td>
                      <td>
                        <span className={sc}>{st}</span>
                      </td>
                      <td>
                        <button
                          type="button"
                          disabled={parsed}
                          onClick={() => parseReplay(record)}
                        >
                          解析
                        </button>
                      </td>
                      <td>
                        <button
                          type="button"
                          disabled={!parsed}
                          onClick={() => {
                            if (!parsed) {
                              alert("仅可播放已解析的录像。");
                              return;
                            }
                            void loadReplayRecord(record);
                          }}
                        >
                          播放
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div
        id="replayDownloadModal"
        className={`settings-modal${replayDownloadOpen ? " open" : ""}`}
        onClick={(e) => {
          if (e.target === e.currentTarget) setReplayDownloadOpen(false);
        }}
      >
        <div className="settings-body" style={{ width: "min(700px, 92vw)" }}>
          <div className="settings-title">
            <span>使用录像 ID 下载录像文件</span>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setReplayDownloadOpen(false)}
            >
              关闭
            </button>
          </div>
          <div className="muted-line">
            输入 Dota2 比赛录像 ID，下载成功后会自动刷新录像列表。
          </div>
          <div className="inline-form">
            <label htmlFor="replayIdInput">录像ID</label>
            <input
              id="replayIdInput"
              type="text"
              inputMode="numeric"
              placeholder="例如：8781301871"
              value={replayIdInput}
              onChange={(e) => setReplayIdInput(e.target.value)}
              disabled={downloadInFlight}
            />
            <button
              type="button"
              onClick={() => void downloadReplayById()}
              disabled={downloadInFlight}
            >
              {downloadInFlight ? "下载中…" : "下载"}
            </button>
          </div>
          <label
            className="small-muted"
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              cursor: "pointer",
              marginTop: 10,
            }}
          >
            <input
              type="checkbox"
              checked={autoParseAfterDownload}
              onChange={(e) => setAutoParseAfterDownload(e.target.checked)}
              disabled={downloadInFlight}
            />
            下载后自动解析
          </label>
          <div className="muted-line" style={{ marginTop: 10 }}>
            {replayDownloadResult}
          </div>
        </div>
      </div>
    </>
  );
}
