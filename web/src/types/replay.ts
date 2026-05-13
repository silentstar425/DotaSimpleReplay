export interface MapBounds {
  min_x: number;
  max_x: number;
  min_y: number;
  max_y: number;
}

export interface HeroState {
  x: number | null;
  y: number | null;
  hp: number;
  max_hp: number;
  mana: number;
  max_mana: number;
  level: number;
  net_worth: number;
  lh: number;
  dn: number;
  total_deaths: number;
}

export interface DeathWindow {
  start_tick: number;
  end_tick: number | null;
}

export interface FinalKda {
  kills: number;
  deaths: number;
  assists: number;
}

export interface PlayerTimeline {
  player_id: number;
  player_name: string;
  hero_name: string;
  team: number;
  final_kda: FinalKda;
  kill_event_ticks: number[];
  ticks: number[];
  states: HeroState[];
  death_windows: DeathWindow[];
}

export interface WorldEntityState {
  x: number | null;
  y: number | null;
  hp: number;
  max_hp: number;
  active: boolean;
}

export interface EntityTimeline {
  entity_id: number;
  entity_name: string;
  class_name: string;
  team: number;
  category: string;
  subtype: string;
  ticks: number[];
  states: WorldEntityState[];
}

export interface GuiPayload {
  dem_path?: string;
  match_id: number;
  game_start_tick: number;
  game_end_tick: number;
  tick_rate: number;
  playback_fps: number;
  tick_game_time_relation?: string;
  map_bounds: MapBounds;
  player_timelines: PlayerTimeline[];
  entity_timelines: EntityTimeline[];
  cache_enabled?: boolean;
  cache_hit?: boolean;
  cache_path?: string;
}

export interface ReplayRecord {
  replay_id: string;
  file_name: string;
  file_path: string;
  dem_path: string;
  downloaded_at: string;
  parsed: boolean;
  parse_error: string;
}

export interface VisionSettings {
  enabled: boolean;
  mode: "both" | "team1" | "team2";
  heroVisionRadius: number;
  fogOpacity: number;
  team1: number;
  team2: number;
}

export interface HeroTrailSettings {
  enabled: boolean;
  sampleEveryTicks: number;
  dotRadius: number;
  durationSec: number;
  fadeOut: boolean;
  selectedHeroes: Set<string>;
}

export interface HeatmapSettings {
  enabled: boolean;
  intervalSec: number;
  radius: number;
  opacity: number;
  durationSec: number;
}

export interface MapViewState {
  zoom: number;
  minZoom: number;
  maxZoom: number;
  panX: number;
  panY: number;
}
