"""Web UI for Idea -> Video. Run: python webui.py  ->  http://127.0.0.1:5000"""
import json
import threading
import time
import traceback
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template, request, send_file

from core import ingest, render, tasks
from core.pipeline import build, resolve_size
from core.script_gen import make_script

ROOT = Path(__file__).parent
app = Flask(__name__)

JOBS = {}          # name -> {"thread": Thread, "log": [str], "status": str, "error": str}
CONFIG_PATH = ROOT / "config.yaml"
SCHED = tasks.Scheduler()


def cfg():
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def save_cfg(c):
    CONFIG_PATH.write_text(yaml.safe_dump(c, allow_unicode=True, sort_keys=False), encoding="utf-8")


def log(job, msg):
    JOBS[job]["log"].append(msg)


def run_job(job, source, mode, fmt, custom, seconds, language):
    try:
        JOBS[job]["status"] = "running"
        log(job, "starting ...")
        final = build(cfg(), ROOT / "output" / job, source, mode, fmt, custom, seconds, language,
                      log=lambda m: log(job, m))
        JOBS[job]["status"] = "done"
        JOBS[job]["final"] = final.name
        log(job, f"DONE -> {final}")
    except Exception as e:
        JOBS[job]["status"] = "error"
        JOBS[job]["error"] = str(e)
        log(job, f"ERROR: {e}")
        traceback.print_exc()


@app.route("/")
def index():
    return render_template("index.html")


# ---------------- ideas + scheduler ----------------

@app.route("/api/ideas", methods=["GET"])
def api_ideas():
    return jsonify(tasks.list_ideas())


@app.route("/api/ideas", methods=["POST"])
def api_ideas_add():
    b = request.json
    texts = b.get("ideas") or []
    if isinstance(texts, str):
        texts = [texts]
    tasks.add_ideas(texts, scheduled_at=b.get("scheduled_at") or None,
                    mode=b.get("mode", "image"), fmt=b.get("format", "shorts"),
                    seconds=int(b.get("seconds", 45)), language=b.get("language", "English"))
    return jsonify({"ok": True})


@app.route("/api/ideas/<int:idea_id>", methods=["DELETE"])
def api_idea_delete(idea_id):
    tasks.delete_idea(idea_id)
    return jsonify({"ok": True})


@app.route("/api/ideas/<int:idea_id>/run", methods=["POST"])
def api_idea_run(idea_id):
    ideas = tasks.list_ideas()
    it = next((i for i in ideas if i["id"] == idea_id), None)
    if not it:
        return jsonify({"error": "not found"}), 404
    if it["status"] == "processing":
        return jsonify({"error": "already processing"}), 400
    tasks.update_idea(idea_id, status="pending", scheduled_at=None)
    return jsonify({"ok": True})


@app.route("/api/scheduler", methods=["GET"])
def api_sched_get():
    c = tasks.sched_config()
    c["running"] = SCHED.running
    return jsonify(c)


@app.route("/api/scheduler/live")
def api_sched_live():
    return jsonify(SCHED.status())


@app.route("/api/scheduler", methods=["POST"])
def api_sched_set():
    b = request.json
    if b.get("action") == "start":
        SCHED.start()
        return jsonify({"ok": True, "running": True})
    if b.get("action") == "stop":
        SCHED.stop()
        return jsonify({"ok": True, "running": False})
    tasks.save_sched_config(b)
    return jsonify({"ok": True})


# ---------------- legacy single-job flow ----------------


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify(cfg())
    c = cfg()
    body = request.json
    for section in ("tts",):
        if section in body:
            c[section].update(body[section])
    if "video" in body:
        for k in ("music", "music_volume", "scene_seconds", "style", "style_video"):
            if k in body["video"]:
                c["video"][k] = body["video"][k]
        if "end_screen" in body["video"]:
            es = c["video"].get("end_screen") or {}
            es.update(body["video"]["end_screen"])
            c["video"]["end_screen"] = es
    save_cfg(c)
    return jsonify({"ok": True})


@app.route("/api/music", methods=["GET"])
def api_music():
    d = ROOT / "music"
    files = [p.name for p in d.iterdir() if p.suffix.lower() in (".mp3", ".wav", ".ogg", ".m4a", ".flac")] if d.is_dir() else []
    return jsonify(files)


@app.route("/api/music/upload", methods=["POST"])
def api_music_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "no file"}), 400
    if not f.filename.lower().endswith((".mp3", ".wav", ".ogg", ".m4a", ".flac")):
        return jsonify({"error": "unsupported format"}), 400
    (ROOT / "music").mkdir(exist_ok=True)
    f.save(ROOT / "music" / f.filename)
    return jsonify({"ok": True})


@app.route("/api/music/delete", methods=["POST"])
def api_music_delete():
    name = Path(request.json["name"]).name
    p = ROOT / "music" / name
    if p.exists():
        p.unlink()
    return jsonify({"ok": True})


@app.route("/api/feed", methods=["POST"])
def api_feed():
    items = ingest.list_feed(request.json["url"])
    return jsonify(items[:25])


@app.route("/api/generate", methods=["POST"])
def api_generate():
    b = request.json
    if not any(b.get(k) for k in ("idea", "url", "feed_item")):
        return jsonify({"error": "provide idea, url or feed item"}), 400
    job = b.get("job") or f"job_{int(time.time())}"
    if b.get("feed_item"):
        source = b["feed_item"]["title"] + "\n\n" + (b["feed_item"].get("summary") or "")
    else:
        source = b.get("idea") or ingest.from_url(b["url"])
    custom = tuple(int(x) for x in b["size"].lower().split("x")) if b.get("size") else None
    t = threading.Thread(target=run_job, args=(job, source, b.get("mode", "image"),
                                               b.get("format", "shorts"), custom,
                                               int(b.get("seconds", 45)), b.get("language", "English")),
                         daemon=True)
    JOBS[job] = {"thread": t, "log": [], "status": "queued", "final": None, "error": None}
    t.start()
    return jsonify({"job": job})


@app.route("/api/status/<job>")
def api_status(job):
    j = JOBS.get(job)
    if not j:
        return jsonify({"error": "unknown job"}), 404
    return jsonify({"status": j["status"], "log": j["log"][-50:], "final": j["final"], "error": j["error"]})


@app.route("/api/jobs")
def api_jobs():
    out = []
    d = ROOT / "output"
    if d.is_dir():
        for p in sorted(d.iterdir(), reverse=True):
            if not p.is_dir():
                continue
            sp = p / "script.json"
            title = ""
            final = []
            if sp.exists():
                try:
                    title = json.loads(sp.read_text(encoding="utf-8")).get("title", "")
                except Exception:
                    pass
            final = [f.name for f in p.glob("*.mp4") if not f.name.startswith(("c", "narration"))]
            out.append({"job": p.name, "title": title, "videos": final})
    return jsonify(out)


@app.route("/api/job/<name>")
def api_job(name):
    p = ROOT / "output" / Path(name).name
    if not p.is_dir():
        return jsonify({"error": "not found"}), 404
    script = None
    if (p / "script.json").exists():
        script = json.loads((p / "script.json").read_text(encoding="utf-8"))
    return jsonify({"job": name, "script": script,
                    "videos": [f.name for f in p.glob("*.mp4") if not f.name.startswith(("c", "narration"))]})


@app.route("/api/job/<name>/rerender", methods=["POST"])
def api_rerender(name):
    """Re-render final video from existing clips (picks up edited script.json/music)."""
    b = request.json or {}
    job = ROOT / "output" / Path(name).name
    script = json.loads((job / "script.json").read_text(encoding="utf-8"))
    c = cfg()
    W, H = resolve_size(c, b.get("format", "shorts"), None)
    v = c["video"]
    clips = sorted(job.glob("c*.mp4"))
    wavs = sorted(job.glob("a*.wav"))
    durations = [render.duration_of(w) for w in wavs] if wavs else [v["scene_seconds"]] * len(clips)

    def work():
        JOBS[name] = {"thread": threading.current_thread(), "log": [], "status": "running", "final": None, "error": None}
        try:
            from core.subtitles import build_ass
            build_ass(script["scenes"], durations, W, H, c, job / "subs.ass")
            import re
            name_out = re.sub(r"[^\w\-]+", "_", script["title"][:50]) + f"_{W}x{H}.mp4"
            music = render.pick_music(ROOT / "music") if v.get("music", True) else None
            JOBS[name]["log"].append(f"render: reassembling (music: {music.name if music else 'none'})")
            es = v.get("end_screen") or {}
            end_screen = {**es, "W": W, "H": H} if (es.get("enabled") and es.get("channel")) else None
            render.assemble(job, clips, wavs, durations, "subs.ass", name_out,
                            v["transition_s"], v["transitions"], music_path=music,
                            music_volume=v.get("music_volume", 0.15), end_screen=end_screen)
            JOBS[name]["status"] = "done"
            JOBS[name]["final"] = name_out
        except Exception as e:
            JOBS[name]["status"] = "error"
            JOBS[name]["error"] = str(e)
    threading.Thread(target=work, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/job/<name>/regen_scene", methods=["POST"])
def api_regen_scene(name):
    idx = int(request.json["scene"])
    job = ROOT / "output" / Path(name).name
    for f in job.glob(f"m{idx:03d}.*"):
        f.unlink()
    return jsonify({"ok": True, "note": "deleted; rerun generate with --job to regenerate"})


@app.route("/api/job/<name>/save_script", methods=["POST"])
def api_save_script(name):
    p = ROOT / "output" / Path(name).name / "script.json"
    p.write_text(json.dumps(request.json, indent=2, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/api/video/<name>/<path:fname>")
def api_video(name, fname):
    p = ROOT / "output" / Path(name).name / Path(fname).name
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(p, mimetype="video/mp4")


if __name__ == "__main__":
    print("Web UI: http://127.0.0.1:5000")
    SCHED.start()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
