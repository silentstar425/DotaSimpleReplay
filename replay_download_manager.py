"""多任务录像下载：并发槽位、暂停/继续、进度与临时文件清理。"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import urllib.error
import urllib.request

from replay_cache import delete_replay_cache
from replay_download_io import (
    REPLAY_DL_UA,
    fetch_opendota_match,
    materialize_downloaded_to_dem,
    replay_storage_root,
    sanitize_stem,
    sniff_downloaded_kind,
    valve_replay_download_url,
)

DOWNLOAD_PREFS_JSON = Path(__file__).resolve().parent / ".dsr_download_prefs.json"


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


class DownloadTask:
    __slots__ = (
        "id",
        "match_id",
        "display_stem",
        "state",
        "phase",
        "bytes_received",
        "bytes_total",
        "download_started_at",
        "created_at",
        "error_message",
        "output_dem_path",
        "download_tmp",
        "go_event",
        "cancelled",
        "thread",
        "_lock",
    )

    def __init__(self, task_id: str, match_id: int, display_stem: str) -> None:
        self.id = task_id
        self.match_id = int(match_id)
        self.display_stem = sanitize_stem(display_stem) if display_stem else str(self.match_id)
        self.state = "queued"
        self.phase = "queued"
        self.bytes_received = 0
        self.bytes_total: int | None = None
        self.download_started_at: float | None = None
        self.created_at = time.time()
        self.error_message: str | None = None
        self.output_dem_path: str | None = None
        self.download_tmp: Path = replay_storage_root() / f"_dl_{task_id}.bin"
        self.go_event = threading.Event()
        self.go_event.set()
        self.cancelled = False
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def dem_out_path(self) -> Path:
        base = sanitize_stem(self.display_stem)
        return replay_storage_root() / f"{base}_{self.match_id}.dem"

    def progress(self) -> float:
        if self.state == "completed":
            return 1.0
        if self.phase == "decompress":
            return 0.95
        if self.bytes_total and self.bytes_total > 0:
            return min(0.94, max(0.0, self.bytes_received / self.bytes_total))
        return 0.0

    def eta_seconds(self) -> float | None:
        if self.bytes_total is None or self.bytes_total <= 0:
            return None
        if self.download_started_at is None:
            return None
        now = time.time()
        elapsed = now - self.download_started_at
        if elapsed < 0.4:
            return None
        rate = self.bytes_received / elapsed
        if rate <= 0:
            return None
        rem = (self.bytes_total - self.bytes_received) / rate
        if rem < 0 or rem > 86400 * 7:
            return None
        return rem

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "match_id": self.match_id,
                "display_stem": self.display_stem,
                "state": self.state,
                "phase": self.phase,
                "bytes_received": self.bytes_received,
                "bytes_total": self.bytes_total,
                "progress": round(self.progress(), 4),
                "eta_seconds": self.eta_seconds(),
                "created_at": _iso(self.created_at),
                "download_started_at": _iso(self.download_started_at),
                "error_message": self.error_message,
                "output_dem_path": self.output_dem_path,
            }


class DownloadTaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, DownloadTask] = {}
        self._lock = threading.Lock()
        self._max_concurrent = 3
        self._active_slots = 0
        self._slot_cv = threading.Condition(self._lock)
        self._auto_parse_after_download = False
        self._auto_parse_cb: Callable[[Path], None] | None = None
        self._load_auto_parse_pref()

    def _load_auto_parse_pref(self) -> None:
        try:
            if DOWNLOAD_PREFS_JSON.is_file():
                raw = json.loads(DOWNLOAD_PREFS_JSON.read_text(encoding="utf-8"))
                self._auto_parse_after_download = bool(raw.get("auto_parse_after_download", False))
        except (OSError, json.JSONDecodeError, TypeError):
            self._auto_parse_after_download = False

    def _save_auto_parse_pref(self) -> None:
        try:
            DOWNLOAD_PREFS_JSON.write_text(
                json.dumps({"auto_parse_after_download": bool(self._auto_parse_after_download)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def set_auto_parse_callback(self, fn: Callable[[Path], None] | None) -> None:
        self._auto_parse_cb = fn

    def get_auto_parse_after_download(self) -> bool:
        with self._lock:
            return bool(self._auto_parse_after_download)

    def set_auto_parse_after_download(self, v: bool) -> bool:
        with self._lock:
            self._auto_parse_after_download = bool(v)
            self._save_auto_parse_pref()
            out = bool(self._auto_parse_after_download)
        print(f"[dl] 下载后自动解析已设为 {out}")
        return out

    @staticmethod
    def _safe_auto_parse(cb: Callable[[Path], None], dem: Path) -> None:
        try:
            print(f"[dl] 下载后自动解析开始 dem={dem}")
            cb(dem)
            print(f"[dl] 下载后自动解析完成 dem={dem}")
        except Exception as ex:
            print(f"[dl] 下载后自动解析失败 dem={dem}: {ex}")

    def set_max_concurrent(self, n: int) -> int:
        n = max(1, min(5, int(n)))
        with self._slot_cv:
            self._max_concurrent = n
            self._slot_cv.notify_all()
        print(f"[dl] 并发槽位上限已更新 max_concurrent={self._max_concurrent}")
        return self._max_concurrent

    def get_max_concurrent(self) -> int:
        with self._lock:
            return self._max_concurrent

    def create_task(self, match_id: int, display_stem: str | None = None) -> DownloadTask:
        try:
            mid = int(match_id)
        except (TypeError, ValueError) as e:
            raise ValueError("比赛编号无效") from e
        if mid <= 0:
            raise ValueError("比赛编号无效")
        tid = uuid.uuid4().hex
        stem = display_stem if display_stem is not None else str(mid)
        task = DownloadTask(tid, mid, stem)
        with self._lock:
            self._tasks[tid] = task
        th = threading.Thread(target=self._run_task, args=(tid,), name=f"dl-{tid[:8]}", daemon=True)
        task.thread = th
        th.start()
        print(f"[dl] 已排队下载任务 id={tid} match_id={mid} display_stem={stem!r}")
        return task

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._tasks.values())
        items.sort(key=lambda t: t.created_at, reverse=True)
        return [t.to_dict() for t in items]

    def update_display_stem(self, task_id: str, stem: str) -> None:
        with self._lock:
            if task_id not in self._tasks:
                raise KeyError("任务不存在")
            t = self._tasks[task_id]
        new_stem = sanitize_stem(stem)
        with t._lock:
            old_out = t.output_dem_path
            old_state = t.state
            t.display_stem = new_stem
        if old_state == "completed" and old_out:
            old_dem = Path(old_out)
            if old_dem.exists():
                new_path = t.dem_out_path()
                if new_path.resolve() != old_dem.resolve():
                    try:
                        if new_path.exists():
                            new_path.unlink()
                        old_dem.replace(new_path)
                        with t._lock:
                            t.output_dem_path = str(new_path.resolve())
                    except OSError:
                        pass

    def pause(self, task_id: str) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
        if not t:
            raise KeyError("任务不存在")
        if t.state in ("completed", "error", "cancelled"):
            print(f"[dl] pause 跳过终态任务 id={task_id} state={t.state}")
            return
        t.go_event.clear()
        with self._slot_cv:
            t.state = "paused"
            self._slot_cv.notify_all()
        print(f"[dl] 任务已暂停 id={task_id} phase={t.phase}")

    def resume(self, task_id: str) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
        if not t:
            raise KeyError("任务不存在")
        if t.state in ("completed", "error", "cancelled"):
            print(f"[dl] resume 跳过终态任务 id={task_id} state={t.state}")
            return
        t.go_event.set()
        with self._slot_cv:
            if t.state == "paused":
                t.state = "running"
            self._slot_cv.notify_all()
        print(f"[dl] 任务已继续 id={task_id} phase={t.phase}")

    def delete_task(self, task_id: str) -> None:
        with self._lock:
            t = self._tasks.pop(task_id, None)
        if not t:
            raise KeyError("任务不存在")
        print(f"[dl] 删除任务 id={task_id} state={t.state}")
        t.cancelled = True
        t.go_event.set()
        if t.thread and t.thread.is_alive():
            t.thread.join(timeout=8.0)
        self._delete_task_files(t)

    def _delete_task_files(self, t: DownloadTask) -> None:
        part = t.download_tmp.parent / (t.download_tmp.name + ".part")
        for p in (part, t.download_tmp):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        if t.output_dem_path:
            op = Path(t.output_dem_path)
            if op.exists():
                try:
                    delete_replay_cache(op)
                except OSError:
                    pass
                try:
                    op.unlink()
                except OSError:
                    pass

    def _wait_until_can_run(self, task: DownloadTask) -> bool:
        """等待并发槽位且未暂停；返回 False 表示已取消。"""
        with self._slot_cv:
            while not task.cancelled:
                slot_ok = self._active_slots < self._max_concurrent
                go_ok = task.go_event.is_set()
                if slot_ok and go_ok:
                    self._active_slots += 1
                    return True
                task.state = "waiting_slot" if not slot_ok else "paused"
                self._slot_cv.wait(timeout=0.2)
            return False

    def _release_slot(self) -> None:
        with self._slot_cv:
            self._active_slots = max(0, self._active_slots - 1)
            self._slot_cv.notify_all()

    def _run_task(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
        if not task:
            return
        print(f"[dl] 工作线程启动 task_id={task_id} match_id={task.match_id}")
        acquired_slot = False
        dem_for_auto: Path | None = None
        try:
            if not self._wait_until_can_run(task):
                task.state = "cancelled"
                print(f"[dl] 任务未获得槽位即取消 id={task_id}")
                return
            acquired_slot = True
            task.state = "running"
            task.phase = "meta"
            task.go_event.wait()
            if task.cancelled:
                task.state = "cancelled"
                print(f"[dl] 任务在元数据前取消 id={task_id}")
                return
            m = fetch_opendota_match(task.match_id)
            if task.cancelled:
                task.state = "cancelled"
                return
            task.go_event.wait()
            if task.cancelled:
                task.state = "cancelled"
                return
            if not isinstance(m, dict):
                raise RuntimeError("OpenDota 返回数据异常")
            cluster = m.get("cluster")
            salt = m.get("replay_salt")
            if cluster is None or salt is None:
                raise RuntimeError("该比赛无公开回放元数据（replay_salt/cluster 缺失），无法下载")
            url = valve_replay_download_url(int(cluster), task.match_id, int(salt))
            print(f"[dl] 开始下载 id={task_id} match_id={task.match_id} url={url[:120]}...")
            task.phase = "download"
            task.download_started_at = time.time()
            self._download_streaming(task, url)
            if task.cancelled:
                task.state = "cancelled"
                return
            task.go_event.wait()
            if task.cancelled:
                task.state = "cancelled"
                return
            if task.download_tmp.stat().st_size < 4096:
                task.download_tmp.unlink(missing_ok=True)
                raise RuntimeError("下载文件过小，可能不是有效录像")
            task.phase = "decompress"
            dem_out = task.dem_out_path()
            materialize_downloaded_to_dem(task.download_tmp, dem_out)
            task.output_dem_path = str(dem_out.resolve())
            dem_for_auto = dem_out.resolve()
            task.state = "completed"
            task.phase = "done"
            task.bytes_received = task.bytes_total or task.bytes_received
            print(f"[dl] 任务完成 id={task_id} dem={task.output_dem_path} size={task.bytes_received}")
        except Exception as e:
            if task.cancelled:
                task.state = "cancelled"
                print(f"[dl] 任务线程结束(已取消) id={task_id}")
            else:
                task.state = "error"
                task.error_message = str(e)
                print(f"[dl] 任务失败 id={task_id}: {e}")
            task.download_tmp.unlink(missing_ok=True)
        finally:
            if acquired_slot:
                self._release_slot()
            do_auto = False
            dem_snap: Path | None = None
            cb_snap: Callable[[Path], None] | None = None
            with self._lock:
                if dem_for_auto is not None and self._auto_parse_after_download:
                    do_auto = True
                    dem_snap = dem_for_auto
                    cb_snap = self._auto_parse_cb
            if do_auto and cb_snap is not None and dem_snap is not None:
                threading.Thread(
                    target=lambda: DownloadTaskManager._safe_auto_parse(cb_snap, dem_snap),
                    name=f"autoparse-{task_id[:8]}",
                    daemon=True,
                ).start()

    def _download_streaming(self, task: DownloadTask, url: str) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": REPLAY_DL_UA})
        resp = urllib.request.urlopen(req, timeout=120)
        try:
            cl = resp.headers.get("Content-Length")
            if cl:
                try:
                    task.bytes_total = int(cl)
                except ValueError:
                    task.bytes_total = None
            chunk = 256 * 1024
            task.download_tmp.parent.mkdir(parents=True, exist_ok=True)
            part = task.download_tmp.parent / (task.download_tmp.name + ".part")
            if part.exists():
                part.unlink()
            received = 0
            with part.open("wb") as out:
                while not task.cancelled:
                    task.go_event.wait()
                    if task.cancelled:
                        break
                    data = resp.read(chunk)
                    if not data:
                        break
                    out.write(data)
                    received += len(data)
                    with task._lock:
                        task.bytes_received = received
            resp.close()
            if task.cancelled:
                part.unlink(missing_ok=True)
                return
            if task.download_tmp.exists():
                task.download_tmp.unlink(missing_ok=True)
            part.replace(task.download_tmp)
            if sniff_downloaded_kind(task.download_tmp) == "html":
                task.download_tmp.unlink(missing_ok=True)
                raise RuntimeError("下载内容不是录像文件")
        finally:
            try:
                resp.close()
            except Exception:
                pass


# Optional type alias for load callback
LoadReplayFn = Callable[[Path, int], tuple[dict[str, Any], Path]]
