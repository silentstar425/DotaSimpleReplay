# Dota 2 回放按 Tick 导出脚本

本仓库提供 `extract_replay_ticks.py`，用于解析 Source 2 Dota 2 回放（`.dem` / `.dem.bz2`）并输出按 tick 组织的 JSON 数据。

## 功能

1. 提取回放中可解析的所有信息表（基于 `gem-dota` 解析结果）
2. 统一按每个 tick 输出到一个 JSON 文件
3. 支持按变量名筛选字段
4. 支持按游戏时间筛选提取范围
5. 自动生成变量说明表（Markdown）

## 使用 uv 管理环境（推荐）

项目已切换为 `uv` 管理 Python 环境与依赖，包含：

- `pyproject.toml`：项目依赖声明
- `uv.lock`：锁定依赖版本
- `.python-version`：建议 Python 版本（3.12）

常用命令：

```bash
# 1) 安装 uv（若本机尚未安装）
pip3 install --user uv

# 2) 根据锁文件创建/同步虚拟环境
uv sync

# 3) 在 uv 环境中运行脚本
uv run python extract_replay_ticks.py <input.dem|input.dem.bz2>
uv run python run.py <input.dem|input.dem.bz2>
uv run python replay_position_gui_tk.py <input.dem|input.dem.bz2>
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

## 简易 GUI 回放（英雄位置）

新增脚本：`run.py`

当前增强版功能：

- 从 `tick=0` 开始播放（不是从游戏时间 0 对应的 `game_start_tick` 开始）
- 地图上展示英雄位置（2D 归一化坐标）
- 按游戏时间 `1x` 速度播放，默认刷新率 `30 FPS`（可调）
- 播放/暂停按钮
- 可拖动进度条（按 tick 跳转）
- 左侧看板（下拉选择并按降序展示）：
  - 资产总额
  - K/D/A
  - 正补/反补
  - 等级
- 右侧英雄状态：
  - 每位英雄 HP / MP
  - 死亡时显示复活剩余时间
- 英雄死亡时不在地图上显示对应图标

该 GUI 为浏览器版，不依赖本地桌面 GUI 库。

### 启动方式

```bash
# 指定回放
python3 run.py /path/to/replay.dem

# 也支持 .dem.bz2（会自动解压）
python3 run.py /path/to/replay.dem.bz2
```

如果不传路径，脚本会尝试读取 `replay_samples/` 下的第一个回放文件。

默认会启动本地 Web 服务并自动打开浏览器，访问地址通常是：

- `http://127.0.0.1:8765/`

可选参数示例：

```bash
# 仅解析并导出 GUI 数据，不启动服务
python3 run.py replay.dem --no-server --export-json output/gui_payload.json

# 指定监听地址和端口
python3 run.py replay.dem --host 0.0.0.0 --port 9000

# 指定默认播放刷新率（FPS）
python3 run.py replay.dem --fps 30
```

说明：

- `tick_rate` 表示“每秒游戏模拟 tick 数”（数据时间轴频率，通常接近 30），不是 UI 的绘制刷新率。
- GUI 播放刷新率由 `FPS` 控制，定义为：**游戏时间每过 1 秒，界面重绘多少次**。
- 调整 `FPS` 不会改变游戏时间推进速度（仍按 1x 实时推进），只会改变每秒更新次数与观感流畅度。
- 已增加解析缓存：首次解析会将播放必要数据写入缓存文件（`.replay_cache/*.pkl`），后续播放同一录像优先读取缓存以提速。
- GUI 提供“清理缓存”按钮，删除当前录像对应缓存前会进行二次确认。

## Tkinter 版 GUI 回放（英雄位置）

新增脚本：`replay_position_gui_tk.py`

功能与浏览器增强版一致：

- 从 `tick=0` 开始播放
- 展示英雄位置（死亡不显示）
- 左侧看板 + 右侧状态面板
- 默认 `30 FPS`，可调（仅影响每秒重绘次数，不改变游戏时间流速）
- 播放/暂停按钮
- 可拖动进度条（按 tick 跳转）

启动方式：

```bash
# 指定回放
python3 replay_position_gui_tk.py /path/to/replay.dem

# 也支持 .dem.bz2（会自动解压）
python3 replay_position_gui_tk.py /path/to/replay.dem.bz2
```

可选参数：

```bash
# 自定义窗口大小
python3 replay_position_gui_tk.py replay.dem --width 1000 --height 1000

# 指定默认 FPS
python3 replay_position_gui_tk.py replay.dem --fps 30
```

说明：

- Tkinter 版本依赖本地 Python 的 `tkinter` 模块（通常由系统包 `python3-tk` 提供）。
- 按你的要求，该版本代码已开发完成，但不在云端环境执行测试。
- Tkinter 版同样支持解析缓存与“清理缓存（二次确认）”。

## 测试回放文件

项目中保留了测试目录与样例回放文件：

- 目录：`replay_samples/`
- 文件：`replay_samples/test_replay_8781301871.dem.bz2`

说明：

- 该文件可直接用于 `extract_replay_ticks.py` 与两个 GUI 脚本的快速验证。
- 选择保留 `.dem.bz2` 压缩格式，以减少仓库体积；脚本会自动解压为 `.dem`（若需要）。

## 素材目录

为便于多人协作，项目素材统一放在 `assets/` 目录下：

- `assets/maps/`：地图相关素材（例如 `map_full.png`）
- `assets/heroes/icons/`：英雄图标
- `assets/ui/backgrounds/`：UI 背景素材
- `assets/ui/widgets/`：UI 组件素材

目录细化规范可见 `assets/README.md`。
