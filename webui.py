"""Web UI for Idea -> Video. Run: python webui.py  ->  http://127.0.0.1:5000"""
import json
import re
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from core import ingest, render, tasks
from core.captions import prepare_captions
from core.comfy import ComfyClient
from core.language import normalize_language, resolve_subtitle_language, resolve_voice
from core.pipeline import MAX_DURATION_REVISIONS, build, prepare_voiceover, resolve_size
from core.script_gen import revise_script_for_duration, script_was_edited
from core.seo import fallback_metadata, generate_metadata
from core.timing import duration_tolerance, narration_target_seconds

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")

JOBS = {}          # name -> {"thread": Thread, "log": [str], "status": str, "error": str}
CONFIG_PATH = ROOT / "config.yaml"
SCHED = tasks.Scheduler()


def cfg():
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def save_cfg(c):
    CONFIG_PATH.write_text(yaml.safe_dump(c, allow_unicode=True, sort_keys=False), encoding="utf-8")


def normalize_service_url(value, label):
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{label} URL cannot be empty.")
    if not value.startswith(("http://", "https://")):
        value = "http://" + value
    parsed = urlparse(value)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"Enter a valid HTTP or HTTPS URL for {label}.") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname or any(char.isspace() for char in parsed.netloc):
        raise ValueError(f"Enter a valid HTTP or HTTPS URL for {label}.")
    return value.rstrip("/")


def log(job, msg):
    entry = JOBS.get(job)
    if not entry:
        return
    entry.setdefault("log", []).append(str(msg))
    entry["log"] = entry["log"][-100:]
    progress, stage = tasks._progress_for_log(str(msg), entry.get("progress", 0))
    entry["progress"], entry["stage"] = progress, stage


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
    try:
        language = normalize_language(b.get("language", "English"), strict=True)
        subtitle_language = str(b.get("subtitle_language", "auto") or "auto").strip()
        resolve_subtitle_language(language, subtitle_language)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    created = tasks.add_ideas(
        texts, scheduled_at=b.get("scheduled_at") or None,
        mode=mode, fmt=fmt, seconds=seconds,
        language=language, subtitle_language=subtitle_language,
    )
    return jsonify({"ok": True, "created": len(created), "ideas": created})


@app.route("/api/ideas/<int:idea_id>", methods=["DELETE"])
def api_idea_delete(idea_id):
    if not tasks.delete_idea(idea_id):
        return jsonify({"error": "a processing idea cannot be removed"}), 409
    return jsonify({"ok": True})


@app.route("/api/ideas/<int:idea_id>/run", methods=["POST"])
def api_idea_run(idea_id):
    ideas = tasks.list_ideas()
    it = next((i for i in ideas if i["id"] == idea_id), None)
    if not it:
        return jsonify({"error": "not found"}), 404
    if it["status"] == "processing":
        return jsonify({"error": "already processing"}), 400
    tasks.update_idea(idea_id, status="pending", scheduled_at=None, progress=0,
                      stage="Queued to run now", error=None, logs=[])
    SCHED.start()
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
    try:
        if "ollama" in body and isinstance(body["ollama"], dict):
            ollama = body["ollama"]
            if "host" in ollama:
                c["ollama"]["host"] = normalize_service_url(ollama["host"], "Ollama")
            if "model" in ollama:
                model = str(ollama["model"]).strip()
                if not model or len(model) > 160:
                    raise ValueError("Enter a valid installed Ollama model tag.")
                c["ollama"]["model"] = model
            if "num_predict" in ollama:
                c["ollama"]["num_predict"] = min(16384, max(512, int(ollama["num_predict"])))
            if "think" in ollama:
                think = ollama["think"]
                if isinstance(think, bool):
                    c["ollama"]["think"] = think
                elif str(think).strip().lower() in ("true", "1", "yes", "on"):
                    c["ollama"]["think"] = True
                elif str(think).strip().lower() in ("false", "0", "no", "off"):
                    c["ollama"]["think"] = False
                else:
                    raise ValueError("Ollama thinking mode must be true or false.")

        if "comfy" in body and isinstance(body["comfy"], dict):
            comfy = body["comfy"]
            if "host" in comfy:
                c["comfy"]["host"] = normalize_service_url(comfy["host"], "ComfyUI")
            if "timeout_s" in comfy:
                c["comfy"]["timeout_s"] = min(7200, max(60, int(comfy["timeout_s"])))
            for mode in ("image", "video"):
                if mode in comfy and isinstance(comfy[mode], dict):
                    fixed = c["comfy"][mode].setdefault("fixed", {})
                    if "megapixels" in comfy[mode]:
                        requested_mp = float(comfy[mode]["megapixels"])
                        upper_mp = 1.0 if mode == "video" else 4.0
                        fixed["megapixels"] = min(upper_mp, max(1.0, requested_mp))
                    if mode == "video" and "turbo" in comfy[mode]:
                        turbo = comfy[mode]["turbo"]
                        if isinstance(turbo, bool):
                            fixed["turbo"] = turbo
                        elif str(turbo).strip().lower() in ("true", "1", "yes", "on"):
                            fixed["turbo"] = True
                        elif str(turbo).strip().lower() in ("false", "0", "no", "off"):
                            fixed["turbo"] = False
                        else:
                            raise ValueError("MiniMax H3 turbo mode must be true or false.")

        if "tts" in body and isinstance(body["tts"], dict):
            tts_patch = body["tts"]
            for key in ("voice", "bangla_voice"):
                if key in tts_patch:
                    voice = str(tts_patch[key]).strip()
                    if not voice or len(voice) > 180:
                        raise ValueError(f"Enter a valid {key.replace('_', ' ')} name.")
                    c["tts"][key] = voice
            for key in ("rate", "bangla_rate"):
                if key in tts_patch:
                    rate = str(tts_patch[key]).strip()
                    if not re.fullmatch(r"[+-](?:[0-9]|[1-9][0-9]|100)%", rate):
                        raise ValueError(f"{key.replace('_', ' ').capitalize()} must be a percentage such as -5% or +0%.")
                    c["tts"][key] = rate
        if "video" in body and isinstance(body["video"], dict):
            for key in ("music", "music_volume", "scene_seconds", "narration_wpm", "bangla_narration_wpm", "style", "style_video"):
                if key in body["video"]:
                    c["video"][key] = body["video"][key]
            try:
                c["video"]["music_volume"] = min(1.0, max(0.0, float(c["video"].get("music_volume", 0.5))))
                c["video"]["narration_wpm"] = min(260, max(100, int(c["video"].get("narration_wpm", 190))))
                c["video"]["bangla_narration_wpm"] = min(260, max(100, int(c["video"].get("bangla_narration_wpm", 155))))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid generation setting: {exc}") from exc
            if "end_screen" in body["video"] and isinstance(body["video"]["end_screen"], dict):
                es = c["video"].get("end_screen") or {}
                es.update(body["video"]["end_screen"])
                try:
                    es["seconds"] = min(15, max(1, int(es.get("seconds", 4))))
                except (TypeError, ValueError) as exc:
                    raise ValueError("Outro length must be a number from 1 to 15 seconds.") from exc
                c["video"]["end_screen"] = es
        if "subtitles" in body and isinstance(body["subtitles"], dict):
            font = str(body["subtitles"].get("font_bangla", c.get("subtitles", {}).get("font_bangla", "Nirmala UI"))).strip()
            if not font or len(font) > 80 or any(char in font for char in ",\r\n"):
                raise ValueError("Enter a valid installed Bangla subtitle font name.")
            c.setdefault("subtitles", {})["font_bangla"] = font
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    save_cfg(c)
    return jsonify({"ok": True})


@app.route("/api/admin/test-workflow", methods=["POST"])
def api_admin_test_workflow():
    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode", "video")).strip().lower()
    if mode not in ("image", "video"):
        return jsonify({"ok": False, "errors": ["Choose image or video mode."]}), 400
    try:
        service = cfg().get("comfy", {})
        report = ComfyClient(service["host"], service.get("timeout_s", 7200)).diagnostics(service[mode])
        return jsonify(report)
    except Exception as exc:
        return jsonify({"ok": False, "errors": [str(exc)], "warnings": [], "missing_models": []})


@app.route("/api/admin/test-connections", methods=["POST"])
def api_admin_test_connections():
    c = cfg()
    results = {}
    ollama = c.get("ollama", {})
    try:
        response = requests.get(f"{ollama['host'].rstrip('/')}/api/tags", timeout=6)
        response.raise_for_status()
        models = response.json().get("models", [])
        names = [item.get("name", "") for item in models]
        selected = str(ollama.get("model", ""))
        results["ollama"] = {
            "ok": True,
            "model": selected,
            "model_installed": selected in names,
            "models": names[:40],
        }
    except Exception as exc:
        results["ollama"] = {"ok": False, "error": str(exc)}

    comfy = c.get("comfy", {})
    try:
        response = requests.get(f"{comfy['host'].rstrip('/')}/system_stats", timeout=6)
        response.raise_for_status()
        results["comfy"] = {"ok": True}
    except Exception as exc:
        results["comfy"] = {"ok": False, "error": str(exc)}
    return jsonify(results)


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
    if not any(b.get(key) for key in ("idea", "url", "feed_item")):
        return jsonify({"error": "provide an idea, URL or feed item"}), 400
    mode, fmt = b.get("mode", "image"), b.get("format", "shorts")
    if mode not in ("image", "video") or fmt not in ("shorts", "video", "custom"):
        return jsonify({"error": "unsupported generation mode or format"}), 400
    try:
        seconds = int(b.get("seconds", 45))
        narration_target_seconds(cfg()["video"], seconds)
        language = normalize_language(b.get("language", "English"), strict=True)
        subtitle_language = str(b.get("subtitle_language", "auto") or "auto").strip()
        resolve_subtitle_language(language, subtitle_language)
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
        custom = tuple(int(value) for value in b["size"].lower().split("x")) if b.get("size") else None
        if fmt == "custom" and (not custom or len(custom) != 2):
            return jsonify({"error": "custom format requires a WxH size"}), 400
        resolve_size(cfg(), fmt, custom)
    except (ValueError, RuntimeError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
    if not source or len(source) > 12000:
        return jsonify({"error": "idea/source must contain 1–12000 characters"}), 400

    custom_job = str(b.get("job") or "").strip()
    job_names = None
    if custom_job:
        job_name = Path(custom_job).name
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", job_name):
            return jsonify({"error": "invalid job name"}), 400
        existing = tasks.get_idea_by_job(job_name)
        if existing and existing.get("status") in ("pending", "processing"):
            return jsonify({"error": "this job is already queued or running"}), 409
        if job_name in JOBS and JOBS[job_name].get("status") in ("queued", "running"):
            return jsonify({"error": "this job is already running"}), 409
        job_names = [job_name]

    created = tasks.add_ideas(
        [source], mode=mode, fmt=fmt, seconds=seconds, language=language,
        subtitle_language=subtitle_language, origin="single", job_names=job_names,
    )
    if not created:
        return jsonify({"error": "could not add generation to the persistent queue"}), 500
    SCHED.start()
    return jsonify({"job": created[0]["job"], "idea_id": created[0]["id"],
                    "status": "queued", "progress": 0}), 202


@app.route("/api/status/<job>")
def api_status(job):
    j = JOBS.get(job)
    if j:
        return jsonify({"status": j["status"], "log": j.get("log", [])[-100:],
                        "final": j.get("final"), "error": j.get("error"),
                        "progress": j.get("progress", 0), "stage": j.get("stage", "")})
    item = tasks.get_idea_by_job(job)
    if not item:
        return jsonify({"error": "unknown job"}), 404
    statuses = {"pending": "queued", "processing": "running", "completed": "done", "error": "error"}
    return jsonify({
        "status": statuses.get(item.get("status"), item.get("status", "queued")),
        "log": (item.get("logs") or [])[-100:],
        "final": item.get("video"),
        "error": item.get("error"),
        "progress": int(item.get("progress", 0) or 0),
        "stage": item.get("stage", ""),
        "idea_id": item.get("id"),
    })


@app.route("/api/jobs")
def api_jobs():
    out = []
    directory = ROOT / "output"
    queue_by_job = {item.get("job"): item for item in tasks.list_ideas() if item.get("job")}
    if directory.is_dir():
        projects = sorted((path for path in directory.iterdir() if path.is_dir()),
                          key=lambda path: path.stat().st_mtime, reverse=True)
        for project in projects:
            script = {}
            try:
                script_path = project / "script.json"
                if script_path.is_file():
                    script = json.loads(script_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                script = {}
            queue_item = queue_by_job.get(project.name) or {}
            videos = [path.name for path in project.glob("*.mp4")
                      if path.is_file() and not re.fullmatch(r"c\d{3}\.mp4", path.name, re.I)
                      and ".partial." not in path.name]
            if queue_item.get("status") in ("pending", "processing"):
                videos = []  # never offer a half-written MP4 to the browser
            seo = {}
            try:
                seo_path = project / "seo.json"
                if seo_path.is_file():
                    seo = json.loads(seo_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                seo = {}
            out.append({"job": project.name, "title": script.get("title", ""), "videos": videos,
                        "status": queue_item.get("status", "completed" if videos else "incomplete"),
                        "progress": int(queue_item.get("progress", 0) or 0), "seo": seo})
    return jsonify(out)


@app.route("/api/job/<name>")
def api_job(name):
    project = ROOT / "output" / Path(name).name
    if not project.is_dir():
        return jsonify({"error": "not found"}), 404
    script = None
    seo = None
    try:
        script_path = project / "script.json"
        if script_path.is_file():
            script = json.loads(script_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return jsonify({"error": f"saved script could not be read: {exc}"}), 500
    try:
        seo_path = project / "seo.json"
        if seo_path.is_file():
            seo = json.loads(seo_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        seo = None
    videos = [path.name for path in project.glob("*.mp4")
              if path.is_file() and not re.fullmatch(r"c\d{3}\.mp4", path.name, re.I)
              and ".partial." not in path.name]
    return jsonify({"job": project.name, "script": script, "seo": seo, "videos": videos})


@app.route("/api/job/<name>/rerender", methods=["POST"])
def api_rerender(name):
    """Fit saved narration to its target, then export again using saved visuals."""
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
        language = normalize_language(metadata.get("language", "English"))
        subtitle_language = str(metadata.get("subtitle_language", "auto") or "auto")
        fmt = body.get("format") or metadata.get("format") or "shorts"
        output_size = metadata.get("output_size") or (script.get("_render") or {}).get("output_size")
        if output_size and len(output_size) == 2:
            W, H = int(output_size[0]), int(output_size[1])
        else:
            W, H = resolve_size(c, fmt, None)
        requested = float(metadata["requested_seconds"]) if metadata.get("requested_seconds") is not None else None
        target_narration = narration_target_seconds(c["video"], requested) if requested else None
        edited_by_user = script_was_edited(script)
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        return jsonify({"error": f"could not prepare re-render: {exc}"}), 400

    JOBS[job_name] = {"thread": None, "log": [], "status": "queued", "final": None, "error": None,
                      "progress": 0, "stage": "Queued for re-render"}

    def work():
        JOBS[job_name]["thread"] = threading.current_thread()
        JOBS[job_name]["status"] = "running"
        try:
            v = c["video"]
            log_line = lambda message: log(job_name, message)
            current_script = script
            durations, wavs = prepare_voiceover(job, current_script["scenes"], c, log_line, language=language)
            revisions = int((current_script.get("_generator") or {}).get("duration_revisions", 0) or 0)
            if target_narration is not None and edited_by_user:
                log_line("script: manual edits detected; preserving your narration during re-render")
            if target_narration is not None and not edited_by_user:
                actual_narration = sum(durations)
                tolerance = duration_tolerance(v, target_narration)
                while abs(actual_narration - target_narration) > tolerance and revisions < MAX_DURATION_REVISIONS:
                    revisions += 1
                    log_line(
                        f"script: measured narration is {actual_narration:.1f}s; target is "
                        f"{target_narration:.1f}s. Correcting duration ({revisions}/{MAX_DURATION_REVISIONS}) ..."
                    )
                    old_metadata = current_script.get("_generator") or {}
                    revised = revise_script_for_duration(
                        c, "", old_metadata.get("mode", "image"), fmt, requested,
                        target_narration, actual_narration,
                        old_metadata.get("language", "English"), current_script,
                        revisions, log=log_line,
                    )
                    new_metadata = revised.get("_generator") or {}
                    if old_metadata.get("source_hash"):
                        new_metadata["source_hash"] = old_metadata["source_hash"]
                    if output_size:
                        new_metadata["output_size"] = output_size
                    current_script = revised
                    script_path.write_text(json.dumps(current_script, ensure_ascii=False, indent=2), encoding="utf-8")
                    durations, wavs = prepare_voiceover(job, current_script["scenes"], c, log_line, language=language)
                    actual_narration = sum(durations)
                if abs(actual_narration - target_narration) > tolerance:
                    expected_final = actual_narration + (requested - target_narration)
                    log_line(
                        f"warning: narration is still {actual_narration:.1f}s (target {target_narration:.1f}s); "
                        f"expected final runtime about {expected_final:.1f}s vs {requested:.1f}s."
                    )
                else:
                    log_line(f"script: narration fitted to {actual_narration:.1f}s (target {target_narration:.1f}s)")

            current_script.setdefault("_generator", {})["subtitle_language"] = subtitle_language
            script_path.write_text(json.dumps(current_script, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                captions, caption_language = prepare_captions(
                    job, c, current_script, language, subtitle_language, log=log_line,
                )
            except Exception as exc:
                log_line(f"warning: subtitle translation failed; using {language} captions ({exc})")
                captions, caption_language = [scene["narration"] for scene in current_script["scenes"]], language

            clips = []
            for index, duration in enumerate(durations):
                tag = f"{index:03d}"
                media_candidates = list(job.glob(f"m{tag}.*"))
                if not media_candidates:
                    raise RuntimeError(f"Scene {index + 1} has no visual asset. Regenerate its visual before re-rendering.")
                media = media_candidates[0]
                kind = "image" if media.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") else "video"
                clip = job / f"c{tag}.mp4"
                render.scene_clip(
                    media.resolve(), kind, duration + v["transition_s"], W, H, v["fps"], clip, index,
                    crf=v.get("scene_crf", 16), preset=v.get("render_preset", "slow"),
                )
                clips.append(clip)
            from core.subtitles import build_ass
            build_ass(current_script["scenes"], durations, W, H, c, job / "subs.ass",
                      captions=captions, language=caption_language)
            safe_title = re.sub(r"[^\w\-]+", "_", str(current_script.get("title", "video"))[:50]) or "video"
            name_out = f"{safe_title}_{W}x{H}.mp4"
            music = render.pick_music(ROOT / "music") if v.get("music", True) else None
            if music:
                log_line(f"render: reassembling with music bed {music.name} (voice ducking on)")
            elif v.get("music", True):
                log_line("render: music is enabled, but no playable audio tracks were found")
            else:
                log_line("render: reassembling without background music")
            es = v.get("end_screen") or {}
            end_screen = {**es, "W": W, "H": H} if (es.get("enabled") and str(es.get("channel", "")).strip()) else None
            render.assemble(job, clips, wavs, durations, "subs.ass", name_out,
                            v["transition_s"], v["transitions"], music_path=music,
                            music_volume=v.get("music_volume", 0.5), end_screen=end_screen,
                            video_crf=v.get("final_crf", 16), video_preset=v.get("render_preset", "slow"))
            actual = render.duration_of(job / name_out)
            log_line(f"render: final duration {actual:.1f}s" + (f" (target {requested:.1f}s)" if requested else ""))
            log_line("seo: generating YouTube and Facebook metadata ...")
            try:
                seo = generate_metadata(c, current_script, language)
            except Exception as seo_error:
                seo = fallback_metadata(current_script, language)
                seo["warning"] = str(seo_error)[:500]
                log_line(f"warning: AI SEO generation failed; saved basic metadata instead ({seo_error})")
            (job / "seo.json.tmp").write_text(json.dumps(seo, ensure_ascii=False, indent=2), encoding="utf-8")
            (job / "seo.json.tmp").replace(job / "seo.json")
            log_line("seo: YouTube and Facebook metadata saved")
            JOBS[job_name]["progress"] = 100
            JOBS[job_name]["stage"] = "Complete"
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
    project = ROOT / "output" / Path(name).name
    file_name = Path(fname).name
    if Path(file_name).suffix.lower() != ".mp4":
        return jsonify({"error": "unsupported video format"}), 404
    p = project / file_name
    if not project.is_dir() or not p.is_file():
        return jsonify({"error": "not found"}), 404
    response = send_file(p, mimetype="video/mp4", conditional=True, etag=True, max_age=0)
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "private, no-cache, no-store, must-revalidate"
    return response


if __name__ == "__main__":
    recovered = tasks.recover_interrupted_ideas()
    if recovered:
        print(f"Recovered {recovered} unfinished job(s); they will resume from saved assets.")
    print("Web UI: http://127.0.0.1:5000")
    SCHED.start()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
