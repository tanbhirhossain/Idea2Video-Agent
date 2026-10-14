"""SQLite-backed idea queue and sequential local generation scheduler."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import closing
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from .pipeline import build

ROOT = Path(__file__).resolve().parent.parent
IDEAS_DB = ROOT / "output" / "ideas.sqlite3"
IDEAS_FILE = ROOT / "output" / "ideas.json"  # one-time migration source for existing installs
SCHED_FILE = ROOT / "output" / "scheduler.json"

_lock = threading.RLock()
_db_init_lock = threading.RLock()


def _load(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return default


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _persist_item(conn, item):
    conn.execute(
        "UPDATE ideas SET status=?, scheduled_at=?, job=?, progress=?, created=?, data=? WHERE id=?",
        (item.get("status", "pending"), item.get("scheduled_at"), item.get("job"),
         int(item.get("progress", 0) or 0), item.get("created", ""),
         json.dumps(item, ensure_ascii=False, separators=(",", ":")), int(item["id"])),
    )


def _connect():
    IDEAS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(IDEAS_DB, timeout=20, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS ideas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT NOT NULL,
        scheduled_at TEXT,
        job TEXT,
        progress INTEGER NOT NULL DEFAULT 0,
        created TEXT NOT NULL,
        data TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ideas_status_id ON ideas(status, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ideas_job ON ideas(job)")
    conn.execute("CREATE TABLE IF NOT EXISTS studio_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    # Import the previous JSON queue once. Never rewrite it, so interrupted JSON
    # writes cannot corrupt the new transactional store or trigger duplicate imports.
    with _db_init_lock:
        migrated = conn.execute("SELECT value FROM studio_meta WHERE key='legacy_ideas_migrated'").fetchone()
        if not migrated:
            legacy = _load(IDEAS_FILE, [])
            if isinstance(legacy, list):
                conn.execute("BEGIN IMMEDIATE")
                for old in legacy:
                    if not isinstance(old, dict):
                        continue
                    try:
                        idea_id = int(old.get("id") or 0)
                    except (TypeError, ValueError):
                        idea_id = 0
                    created = str(old.get("created") or datetime.now().isoformat(timespec="seconds"))
                    item = {
                        "id": idea_id,
                        "text": str(old.get("text") or "").strip(),
                        "status": old.get("status", "pending"),
                        "scheduled_at": old.get("scheduled_at"),
                        "mode": old.get("mode", "image"),
                        "format": old.get("format", "shorts"),
                        "seconds": int(old.get("seconds", 45) or 45),
                        "language": old.get("language", "English"),
                        "subtitle_language": old.get("subtitle_language", "auto"),
                        "origin": old.get("origin", "queue"),
                        "job": old.get("job") or (f"job_{idea_id}" if idea_id else None),
                        "video": old.get("video"),
                        "error": old.get("error"),
                        "progress": int(old.get("progress", 0) or 0),
                        "stage": old.get("stage", "Queued"),
                        "logs": list(old.get("logs") or []),
                        "created": created,
                        "completed": old.get("completed"),
                    }
                    if not item["text"]:
                        continue
                    cursor = conn.execute(
                        "INSERT OR IGNORE INTO ideas(id,status,scheduled_at,job,progress,created,data) VALUES(?,?,?,?,?,?,?)",
                        (idea_id or None, item["status"], item["scheduled_at"], item["job"],
                         item["progress"], created, json.dumps(item, ensure_ascii=False, separators=(",", ":"))),
                    )
                    if cursor.rowcount and not idea_id:
                        item["id"] = cursor.lastrowid
                        if not item.get("job"):
                            item["job"] = f"job_{item['id']}"
                        _persist_item(conn, item)
                conn.execute("INSERT OR REPLACE INTO studio_meta(key,value) VALUES('legacy_ideas_migrated','1')")
                conn.commit()
            else:
                conn.execute("INSERT OR REPLACE INTO studio_meta(key,value) VALUES('legacy_ideas_migrated','1')")
    return conn


def _decode(row):
    try:
        item = json.loads(row["data"])
        if isinstance(item, dict):
            item["id"] = int(row["id"])
            return item
    except (TypeError, json.JSONDecodeError):
        pass
    return {"id": int(row["id"]), "status": row["status"], "job": row["job"],
            "progress": row["progress"], "created": row["created"], "text": ""}


def _append_log(item, message):
    logs = item.get("logs") if isinstance(item.get("logs"), list) else []
    logs.append(str(message))
    item["logs"] = logs[-100:]


def list_ideas():
    with closing(_connect()) as conn:
        return [_decode(row) for row in conn.execute("SELECT * FROM ideas ORDER BY id").fetchall()]


def get_idea(idea_id):
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM ideas WHERE id=?", (int(idea_id),)).fetchone()
        return _decode(row) if row else None


def get_idea_by_job(job):
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM ideas WHERE job=? ORDER BY id DESC LIMIT 1", (str(job),)).fetchone()
        return _decode(row) if row else None


def add_ideas(texts, scheduled_at=None, mode="image", fmt="shorts", seconds=45,
              language="English", subtitle_language="auto", origin="queue", job_names=None):
    created = []
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for index, text in enumerate(texts):
            text = str(text or "").strip()
            if not text:
                continue
            created_at = datetime.now().isoformat(timespec="seconds")
            requested_job = job_names[index] if job_names and index < len(job_names) else None
            item = {
                "id": 0,
                "text": text,
                "status": "pending",
                "scheduled_at": scheduled_at,
                "mode": mode,
                "format": fmt,
                "seconds": int(seconds),
                "language": str(language or "English"),
                "subtitle_language": str(subtitle_language or "auto"),
                "origin": origin,
                "job": requested_job,
                "video": None,
                "error": None,
                "progress": 0,
                "stage": "Queued",
                "logs": [],
                "created": created_at,
                "completed": None,
            }
            cursor = conn.execute(
                "INSERT INTO ideas(status,scheduled_at,job,progress,created,data) VALUES(?,?,?,?,?,?)",
                ("pending", scheduled_at, requested_job, 0, created_at,
                 json.dumps(item, ensure_ascii=False, separators=(",", ":"))),
            )
            item["id"] = int(cursor.lastrowid)
            if not item["job"]:
                item["job"] = f"job_{item['id']}"
            _persist_item(conn, item)
            created.append(dict(item))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return created


def update_idea(idea_id, **fields):
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM ideas WHERE id=?", (int(idea_id),)).fetchone()
        if not row:
            conn.commit()
            return None
        item = _decode(row)
        item.update(fields)
        _persist_item(conn, item)
        conn.commit()
        return dict(item)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_idea(idea_id):
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM ideas WHERE id=?", (int(idea_id),)).fetchone()
        if row and row["status"] == "processing":
            conn.rollback()
            return False
        conn.execute("DELETE FROM ideas WHERE id=?", (int(idea_id),))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def recover_interrupted_ideas():
    """Requeue work left marked as running by an unclean server shutdown."""
    recovered = 0
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT * FROM ideas WHERE status IN ('processing','running') ORDER BY id").fetchall()
        for row in rows:
            item = _decode(row)
            item.update({"status": "pending", "progress": 0,
                         "stage": "Recovered after restart; resuming from saved assets", "error": None})
            _append_log(item, "scheduler: server restarted; job safely requeued")
            _persist_item(conn, item)
            recovered += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return recovered


# ---------------- scheduler configuration ----------------


def sched_config():
    config = _load(SCHED_FILE, {
        "enabled": False, "daily_time": "09:00", "mode": "image",
        "format": "shorts", "seconds": 45, "language": "English",
        "subtitle_language": "auto",
    })
    return config if isinstance(config, dict) else {"enabled": False, "daily_time": "09:00"}


def save_sched_config(config):
    with _lock:
        old = sched_config()
        old.update(config or {})
        _save(SCHED_FILE, old)
    return old


def _claim_next():
    """Atomically claim the next runnable pending idea across threads/processes."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT * FROM ideas WHERE status='pending' ORDER BY id").fetchall()
        now = datetime.now()
        for row in rows:
            item = _decode(row)
            scheduled = item.get("scheduled_at")
            if scheduled:
                try:
                    when = datetime.fromisoformat(scheduled)
                except (TypeError, ValueError):
                    when = None
                if when and when > now:
                    continue
            item.update({"status": "processing", "progress": 1,
                         "stage": "Starting generation", "error": None})
            _append_log(item, "scheduler: generation started")
            _persist_item(conn, item)
            conn.commit()
            return dict(item)
        conn.commit()
        return None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _progress_for_log(message: str, current: int) -> tuple[int, str]:
    """Map pipeline log milestones to a stable, monotonic 0-99% progress value."""
    text = str(message)
    progress = max(0, min(99, int(current or 0)))
    stage = text[:240]
    match = re.match(r"voice (\d+)/(\d+):", text)
    if match:
        index, total = map(int, match.groups())
        progress = max(progress, 4 + int(13 * max(0, index - 1) / max(1, total)))
        stage = f"Narration · clip {index}/{total}"
    else:
        match = re.match(r"scene (\d+)/(\d+):", text)
        if match:
            index, total = map(int, match.groups())
            if "visual ready" in text or "clip rendered" in text:
                progress = max(progress, 20 + int(66 * index / max(1, total)))
            else:
                progress = max(progress, 20 + int(66 * max(0, index - 1) / max(1, total)))
            stage = f"Scene visuals · {index}/{total}"
        elif text.startswith("script:"):
            progress = max(progress, 3)
            stage = text[:180]
        elif text.startswith("captions:"):
            progress = max(progress, 18)
            stage = text[:180]
        elif text.startswith("render: assembling"):
            progress = max(progress, 89)
            stage = "Assembling the final video"
        elif text.startswith("render: final duration"):
            progress = max(progress, 94)
            stage = "Final video rendered"
        elif text.startswith("seo:"):
            progress = max(progress, 96)
            stage = "Generating platform SEO"
        elif text.lower().startswith("comfyui:"):
            stage = text[:180]
        elif "warning:" in text.lower():
            stage = text[:180]
    return min(99, progress), stage


class Scheduler:
    """Sequential worker for queued ideas, protecting local GPU and media resources."""

    def __init__(self, log=print):
        self.log = log
        self.thread = None
        self.running = False
        self.current = None
        self.last_run = None
        self.logs = []
        self.current_stage = ""
        self.current_progress = 0

    def emit(self, msg):
        message = str(msg)
        self.logs.append((datetime.now().isoformat(timespec="seconds"), message))
        self.logs = self.logs[-300:]
        if self.current is not None:
            item = get_idea(self.current)
            if item:
                if item.get("status") == "completed":
                    progress, stage = 100, "Complete"
                elif item.get("status") == "error":
                    progress, stage = int(item.get("progress", 0)), "Failed"
                else:
                    progress, stage = _progress_for_log(message, item.get("progress", 0))
                logs = list(item.get("logs") or [])
                logs.append(message)
                update_idea(self.current, progress=progress, stage=stage, logs=logs[-100:])
                self.current_progress, self.current_stage = progress, stage
        elif message.startswith("scene "):
            self.current_stage = message
        self.log(message)

    def status(self):
        item = next((entry for entry in list_ideas() if entry.get("status") == "processing"), None)
        current = {key: item.get(key) for key in ("id", "job", "text", "status", "progress", "stage")} if item else None
        return {
            "running": self.running,
            "current": current,
            "stage": item.get("stage", "") if item else self.current_stage,
            "progress": item.get("progress", 0) if item else 0,
            "logs": self.logs[-100:],
            "last_run": self.last_run,
        }

    def start(self):
        if not (self.thread and self.thread.is_alive()):
            self.running = True
            self.thread = threading.Thread(target=self._loop, daemon=True, name="idea2video-scheduler")
            self.thread.start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                config = sched_config()
                if config.get("enabled"):
                    self._enqueue_daily(config)
                idea = _claim_next()
                if idea:
                    self._run_idea(idea, config)
                else:
                    time.sleep(2)
            except Exception:
                traceback.print_exc()
                time.sleep(5)

    def _enqueue_daily(self, config):
        """Make unscheduled pending ideas runnable once daily at the configured time."""
        now = datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            hour, minute = map(int, str(config.get("daily_time", "09:00")).split(":")[:2])
        except (TypeError, ValueError):
            hour, minute = 9, 0
        fire = midnight + timedelta(hours=hour, minutes=minute)
        if now >= fire:
            marker = ROOT / "output" / f".sched_fired_{fire:%Y%m%d}"
            if not marker.exists():
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("fired", encoding="utf-8")
                self.emit(f"scheduler: daily run triggered at {fire:%H:%M}")
                for item in list_ideas():
                    if item.get("status") == "pending" and not item.get("scheduled_at"):
                        update_idea(item["id"], scheduled_at=fire.isoformat(timespec="seconds"))

    def _run_idea(self, idea, config):
        idea_id = idea["id"]
        self.current = idea_id
        self.current_stage = "Starting generation"
        self.current_progress = 1
        job = str(idea.get("job") or f"job_{idea_id}")
        update_idea(idea_id, status="processing", job=job, progress=1,
                    stage=self.current_stage, error=None)
        self.emit(f"scheduler: generating '{idea['text'][:60]}' ...")
        try:
            import yaml
            project_config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
            final = build(
                project_config, ROOT / "output" / job, idea["text"],
                idea.get("mode") or config.get("mode", "image"),
                idea.get("format") or config.get("format", "shorts"),
                None, idea.get("seconds", 45), idea.get("language", "English"),
                subtitle_language=idea.get("subtitle_language", "auto"),
                log=self.emit,
            )
            update_idea(
                idea_id, status="completed", job=job, video=final.name, progress=100,
                stage="Complete", error=None,
                completed=datetime.now().isoformat(timespec="seconds"),
            )
            self.emit(f"scheduler: DONE -> {final}")
        except Exception as exc:
            update_idea(idea_id, status="error", job=job, error=str(exc)[:1000], stage="Failed")
            self.emit(f"scheduler: ERROR for '{idea['text'][:50]}': {exc}")
            traceback.print_exc()
        finally:
            self.last_run = datetime.now().isoformat(timespec="seconds")
            self.current = None
            self.current_stage = ""
            self.current_progress = 0
