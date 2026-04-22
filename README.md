# Dota 2 回放按 Tick 导出脚本

本仓库提供 `extract_replay_ticks.py`，用于解析 Source 2 Dota 2 回放（`.dem` / `.dem.bz2`）并输出按 tick 组织的 JSON 数据。

## 功能

1. 提取回放中可解析的所有信息表（基于 `gem-dota` 解析结果）
2. 统一按每个 tick 输出到一个 JSON 文件
3. 支持按变量名筛选字段
4. 支持按游戏时间筛选提取范围
5. 自动生成变量说明表（Markdown）

## 安装依赖

```bash
pip3 install --user gem-dota
```

## 基本用法

```bash
python3 extract_replay_ticks.py <input.dem|input.dem.bz2>
```

默认输出：

- `replay_tick_data.json`
- `replay_variable_table.md`

## 常用参数

```bash
python3 extract_replay_ticks.py replay.dem \
  --output-json output/all_ticks.json \
  --output-schema output/variable_table.md \
  --variables tick,players.gold,players.net_worth,positions.x,positions.y,player_id,hero_name,team \
  --start-game-time 0 \
  --end-game-time 600 \
  --dense-ticks
```

参数说明：

- `--variables`：逗号分隔变量名。支持两种写法：
  - `col`（例如 `gold`，匹配所有表中的同名列）
  - `table.col`（例如 `players.net_worth`，只匹配指定表）
- `--start-game-time` / `--end-game-time`：按游戏时间（秒）过滤输出区间，可为负数
- `--dense-ticks`：按连续 tick 输出（即使某 tick 无数据，也输出空 `tables`）

## tick 与游戏时间转换关系

脚本中采用如下关系（`game_start_tick` 对应游戏时间 `0s`）：

- `game_time_seconds = (tick - game_start_tick) / tick_rate`
- `tick = round(game_start_tick + game_time_seconds * tick_rate)`

输出 JSON 的 `meta.tick_game_time_relation` 中也会记录该关系。
