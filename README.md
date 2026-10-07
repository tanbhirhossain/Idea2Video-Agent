# Idea -> Video

Setup
1. `pip install -r requirements.txt`; ffmpeg + ffprobe must be on PATH.
2. Ollama running with `qwen3.5:latest`; ComfyUI Desktop running.
3. In ComfyUI: open each workflow -> Export (API) -> save into `workflows/`
   (`image_qwen_api.json`, `video_minimax_api.json`).
4. `python tools/inspect_workflow.py workflows/image_qwen_api.json` -> put the node IDs
   (prompt / width / height / seed) in `config.yaml`. Same for the video workflow.

Run
    python main.py --idea "Rise of the Ottoman Empire" --mode image --format shorts --seconds 45
    python main.py --url https://... --mode video --format video --seconds 120
    python main.py --feed https://feeds.bbci.co.uk/news/technology/rss.xml --mode image
    python main.py --idea "..." --format custom --size 1080x1350

Resume / tweak: reuse `--job output/job_xxx`. Edit `script.json` there, delete any `mNNN.*`
file to regenerate that scene's visual. Finished files are never regenerated.

Background music
    Drop any .mp3/.wav/.ogg/.m4a/.flac files into `music/` — a random track is
    mixed under the narration (volume in config.yaml -> video.music_volume).
    Two synthesized ambient tracks are included (generated, 100% copyright-free).
    Free/royalty-free sources: pixabay.com/music (Pixabay license), incompetech.com
    (CC-BY), freemusicarchive.org, YouTube Audio Library (need attribution for CC-BY).

Web UI
    pip install flask
    python webui.py  ->  http://127.0.0.1:5000
    Create videos (idea/URL/feed), edit scripts, re-render, manage jobs & music.
