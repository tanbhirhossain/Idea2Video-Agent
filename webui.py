"""Web UI for Idea -> Video. Run: python webui.py  ->  http://127.0.0.1:5000"""
import json
import re
import threading
import time
import traceback
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from core import ingest, render, tasks
from core.pipeline import build, prepare_voiceover, resolve_size
from core.timing import narration_target_seconds

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")

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
    b = request.get_json(silent=True) or {}
    texts = b.get("ideas") or []
    if isinstance(texts, str):
        texts = [texts]
    if not isinstance(texts, list) or not any(str(text).strip() for text in texts):
        return jsonify({"error": "add at least one idea"}), 400
    try:
        seconds = int(b.get("seconds", 45))
        narration_target_seconds(cfg()["video"], seconds)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    mode, fmt = b.get("mode", "image"), b.get("format", "shorts")
    if mode not in ("image", "video") or fmt not in ("shorts", "video"):
        return jsonify({"error": "unsupported generation mode or format"}), 400
    tasks.add_ideas(texts, scheduled_at=b.get("scheduled_at") or None,
                    mode=mode, fmt=fmt, seconds=seconds,
                    language=str(b.get("language", "English")))
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
    body = request.get_json(silent=True) or {}
    for section in ("tts",):
        if section in body and isinstance(body[section], dict):
            c[section].update(body[section])
    if "video" in body and isinstance(body["video"], dict):
        for key in ("music", "music_volume", "scene_seconds", "narration_wpm", "style", "style_video"):
            if key in body["video"]:
                c["video"][key] = body["video"][key]
        try:
            c["video"]["music_volume"] = min(1.0, max(0.0, float(c["video"].get("music_volume", 0.15))))
            c["video"]["narration_wpm"] = min(260, max(100, int(c["video"].get("narration_wpm", 190))))
        except (TypeError, ValueError) as exc:
            return jsonify({"error": f"Invalid generation setting: {exc}"}), 400
        if "end_screen" in body["video"] and isinstance(body["video"]["end_screen"], dict):
            es = c["video"].get("end_screen") or {}
            es.update(body["video"]["end_screen"])
            try:
                es["seconds"] = min(15, max(1, int(es.get("seconds", 4))))
            except (TypeError, ValueError):
                return jsonify({"error": "Outro length must be a number from 1 to 15 seconds."}), 400
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
    filename = secure_filename(f.filename)
    if not filename or not filename.lower().endswith((".mp3", ".wav", ".ogg", ".m4a", ".flac")):
        return jsonify({"error": "unsupported format"}), 400
    (ROOT / "music").mkdir(exist_ok=True)
    f.save(ROOT / "music" / filename)
    return jsonify({"ok": True, "name": filename})


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
    b = request.get_json(silent=True) or {}
    if not any(b.get(k) for k in ("idea", "url", "feed_item")):
        return jsonify({"error": "provide an idea, URL or feed item"}), 400
    mode, fmt = b.get("mode", "image"), b.get("format", "shorts")
    if mode not in ("image", "video") or fmt not in ("shorts", "video", "custom"):
        return jsonify({"error": "unsupported generation mode or format"}), 400
    try:
        seconds = int(b.get("seconds", 45))
        narration_target_seconds(cfg()["video"], seconds)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        if b.get("feed_item"):
            item = b["feed_item"]
            source = str(item.get("title", "")) + "\n\n" + str(item.get("summary", ""))
        elif b.get("url"):
            source = ingest.from_url(str(b["url"]))
        else:
            source = str(b.get("idea", "")).strip()
        custom = tuple(int(x) for x in b["size"].lower().split("x")) if b.get("size") else None
        if fmt == "custom" and (not custom or len(custom) != 2):
            return jsonify({"error": "custom format requires a WxH size"}), 400
        resolve_size(cfg(), fmt, custom)
    except (ValueError, RuntimeError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
    requested_job = str(b.get("job") or f"job_{time.time_ns()}")
    job = Path(requested_job).name
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", job):
        return jsonify({"error": "invalid job name"}), 400
    if job in JOBS and JOBS[job].get("status") in ("queued", "running"):
        return jsonify({"error": "this job is already running"}), 409
    t = threading.Thread(target=run_job, args=(
        job, source, mode, fmt, custom, seconds, str(b.get("language", "English"))), daemon=True)
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
    """Refresh narration, scene timing, subtitles, and the final export from a saved script."""
    body = request.get_json(silent=True) or {}
    job_name = Path(name).name
    job = ROOT / "output" / job_name
    script_path = job / "script.json"
    if not script_path.is_file():
        return jsonify({"error": "project or saved script not found"}), 404
    try:
        script = json.loads(script_path.read_text(encoding="utf-8"))
        if not isinstance(script.get("scenes"), list) or len(script["scenes"]) < 2:
            return jsonify({"error": "the script needs at least two scenes"}), 400
        c = cfg()
        metadata = script.get("_generator") or {}
        fmt = body.get("format") or metadata.get("format") or "shorts"
        output_size = metadata.get("output_size") or (script.get("_render") or {}).get("output_size")
        if output_size and len(output_size) == 2:
            W, H = int(output_size[0]), int(output_size[1])
        else:
            W, H = resolve_size(c, fmt, None)
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        return jsonify({"error": f"could not prepare re-render: {exc}"}), 400

    JOBS[job_name] = {"thread": None, "log": [], "status": "queued", "final": None, "error": None}

    def work():
        JOBS[job_name]["thread"] = threading.current_thread()
        JOBS[job_name]["status"] = "running"
        try:
            v = c["video"]
            log_line = lambda message: log(job_name, message)
            durations, wavs = prepare_voiceover(job, script["scenes"], c, log_line)
            clips = []
            for index, duration in enumerate(durations):
                tag = f"{index:03d}"
                media_candidates = list(job.glob(f"m{tag}.*"))
                if not media_candidates:
                    raise RuntimeError(f"Scene {index + 1} has no visual asset. Regenerate its visual before re-rendering.")
                media = media_candidates[0]
                kind = "image" if media.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") else "video"
                clip = job / f"c{tag}.mp4"
                render.scene_clip(media.resolve(), kind, duration + v["transition_s"], W, H, v["fps"], clip, index)
                clips.append(clip)
            from core.subtitles import build_ass
            build_ass(script["scenes"], durations, W, H, c, job / "subs.ass")
            safe_title = re.sub(r"[^\w\-]+", "_", str(script.get("title", "video"))[:50]) or "video"
            name_out = f"{safe_title}_{W}x{H}.mp4"
            music = render.pick_music(ROOT / "music") if v.get("music", True) else None
            log_line(f"render: reassembling (music: {music.name if music else 'none'})")
            es = v.get("end_screen") or {}
            end_screen = {**es, "W": W, "H": H} if (es.get("enabled") and str(es.get("channel", "")).strip()) else None
            render.assemble(job, clips, wavs, durations, "subs.ass", name_out,
                            v["transition_s"], v["transitions"], music_path=music,
                            music_volume=v.get("music_volume", 0.15), end_screen=end_screen)
            actual = render.duration_of(job / name_out)
            requested = metadata.get("requested_seconds")
            log_line(f"render: final duration {actual:.1f}s" + (f" (target {float(requested):.1f}s)" if requested else ""))
            JOBS[job_name]["status"] = "done"
            JOBS[job_name]["final"] = name_out
        except Exception as exc:
            JOBS[job_name]["status"] = "error"
            JOBS[job_name]["error"] = str(exc)
            log(job_name, f"ERROR: {exc}")
            traceback.print_exc()

    thread = threading.Thread(target=work, daemon=True)
    JOBS[job_name]["thread"] = thread
    thread.start()
    return jsonify({"ok": True, "job": job_name})

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
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
