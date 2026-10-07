"""Idea list with statuses + a sequential job scheduler (queue or scheduled time)."""
import json
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from .pipeline import build

ROOT = Path(__file__).parent.parent
IDEAS_FILE = ROOT / "output" / "ideas.json"
SCHED_FILE = ROOT / "output" / "scheduler.json"

_lock = threading.Lock()


def _load(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------- ideas ----------------

def list_ideas():
    with _lock:
        return _load(IDEAS_FILE, [])


def add_ideas(texts, scheduled_at=None, mode="image", fmt="shorts", seconds=45, language="English"):
    with _lock:
        ideas = _load(IDEAS_FILE, [])
        for t in texts:
            t = t.strip()
            if not t:
                continue
            ideas.append({
                "id": int(time.time() * 1000) + len(ideas),
                "text": t, "status": "pending", "scheduled_at": scheduled_at,
                "mode": mode, "format": fmt, "seconds": seconds, "language": language,
                "job": None, "video": None, "error": None,
                "created": datetime.now().isoformat(timespec="seconds"),
                "completed": None,
            })
        _save(IDEAS_FILE, ideas)
    return ideas


def update_idea(idea_id, **fields):
    with _lock:
        ideas = _load(IDEAS_FILE, [])
        for it in ideas:
            if it["id"] == idea_id:
                it.update(fields)
        _save(IDEAS_FILE, ideas)


def delete_idea(idea_id):
    with _lock:
        ideas = [i for i in _load(IDEAS_FILE, []) if i["id"] != idea_id]
        _save(IDEAS_FILE, ideas)


# ---------------- scheduler ----------------

def sched_config():
    cfg = _load(SCHED_FILE, {"enabled": False, "daily_time": "09:00", "mode": "image",
                             "format": "shorts", "seconds": 45, "language": "English"})
    return cfg


def save_sched_config(cfg):
    with _lock:
        old = sched_config()
        old.update(cfg)
        _save(SCHED_FILE, old)
    return old


def _claim_next():
    """Atomically mark the next runnable pending idea as processing; return it or None."""
    with _lock:
        ideas = _load(IDEAS_FILE, [])
        cfg = sched_config()
        now = datetime.now()
        for it in ideas:
            if it["status"] != "pending":
                continue
            s = it.get("scheduled_at")
            if s:
                try:
                    when = datetime.fromisoformat(s)
                except ValueError:
                    when = None
                if when and when > now:
                    continue
            it["status"] = "processing"
            _save(IDEAS_FILE, ideas)
            return it
    return None


class Scheduler:
    """Runs pending ideas one at a time â€” manually queued or at a daily scheduled time."""

    def __init__(self, log=print):
        self.log = log
        self.thread = None
        self.running = False
        self.current = None
        self.last_run = None
        self.logs = []          # (timestamp, message) of scheduler activity
        self.current_stage = "" # e.g. "scene 3/8: generating image ..."

    def emit(self, msg):
        self.logs.append((datetime.now().isoformat(timespec="seconds"), msg))
        self.logs = self.logs[-300:]
        if msg.startswith("scene "):
            self.current_stage = msg
        self.log(msg)

    def status(self):
        ideas = _load(IDEAS_FILE, [])
        cur = next((i for i in ideas if i["status"] == "processing"), None)
        progress = None
        if cur:
            import re
            m = re.match(r"scene (\d+)/(\d+)", self.current_stage or "")
            if m:
                done = int(m.group(1)) - 1
                total = int(m.group(2))
                # scenes phase is ~80% of total work, render ~20%
                progress = min(99, int(done / total * 80))
            else:
                progress = 3
        return {"running": self.running, "current": cur, "stage": self.current_stage,
                "progress": progress, "logs": self.logs[-100:], "last_run": self.last_run}

    def start(self):
        if not (self.thread and self.thread.is_alive()):
            self.running = True
            self.thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
            self.thread.start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                cfg = sched_config()
                if cfg.get("enabled"):
                    self._enqueue_daily(cfg)
                idea = _claim_next()
                if idea:
                    self._run_idea(idea, cfg)
                else:
                    time.sleep(5)
            except Exception:
                traceback.print_exc()
                time.sleep(10)

    def _enqueue_daily(self, cfg):
        """Once per day at daily_time, move all pending ideas into run range by clearing their date."""
        now = datetime.now()
        target = now.replace(hour=0, minute=0, second=0, microsecond=0)
        h, m = map(int, str(cfg.get("daily_time", "09:00")).split(":")[:2])
        fire = target + timedelta(hours=h, minutes=m)
        if now >= fire:
            marker = ROOT / "output" / f".sched_fired_{fire.strftime('%Y%m%d')}"
            if not marker.exists():
                marker.write_text("fired", encoding="utf-8")
                self.emit(f"scheduler: daily run triggered at {fire:%H:%M}")
                # pending ideas with no explicit schedule become runnable now
                with _lock:
                    ideas = _load(IDEAS_FILE, [])
                    for it in ideas:
                        if it["status"] == "pending" and not it.get("scheduled_at"):
                            it["scheduled_at"] = fire.isoformat(timespec="seconds")
                    _save(IDEAS_FILE, ideas)

    def _run_idea(self, idea, cfg):
        self.current = idea["id"]
        update_idea(idea["id"], status="processing", error=None)
        self.emit(f"scheduler: generating '{idea['text'][:60]}' ...")
        job = f"job_{idea['id']}"
        try:
            import yaml
            c = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
            final = build(c, ROOT / "output" / job, idea["text"],
                          idea.get("mode") or cfg.get("mode", "image"),
                          idea.get("format") or cfg.get("format", "shorts"),
                          None, idea.get("seconds", 45), idea.get("language", "English"),
                          log=self.emit)
            update_idea(idea["id"], status="completed", job=job, video=final.name,
                        completed=datetime.now().isoformat(timespec="seconds"))
            self.emit(f"scheduler: DONE -> {final}")
        except Exception as e:
            update_idea(idea["id"], status="error", error=str(e)[:500])
            self.emit(f"scheduler: ERROR for '{idea['text'][:50]}': {e}")
            traceback.print_exc()
        finally:
            self.last_run = datetime.now().isoformat(timespec="seconds")
            self.current = None

