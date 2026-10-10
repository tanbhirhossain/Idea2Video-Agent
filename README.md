# Idea → Video Studio

Turn an idea, article, or RSS item into a narrated video using Ollama, ComfyUI, Edge TTS, and FFmpeg.

## Setup

1. Install Python dependencies: `pip install -r requirements.txt`.
2. Install FFmpeg and FFprobe and make sure both are on `PATH`.
3. Start Ollama with the model configured in `config.yaml` and start ComfyUI.
4. Check the ComfyUI workflow files and node maps in `config.yaml` match your local ComfyUI installation. Use `python tools/inspect_workflow.py <workflow.json>` to inspect node IDs if you change workflows.

The repository includes the workflow JSON files referenced by the default config. If you export updated API workflows, save them under `workflows/` and update the corresponding workflow paths and node maps in `config.yaml`.

## Run from the command line

```bash
python main.py --idea "Rise of the Ottoman Empire" --mode image --format shorts --seconds 65
python main.py --url https://example.com/article --mode video --format video --seconds 120
python main.py --feed https://feeds.bbci.co.uk/news/technology/rss.xml --mode image
python main.py --idea "..." --format custom --size 1080x1350
```

A requested duration is the **finished export duration**, including transition padding and the optional end card. The pipeline budgets narration to fit, measures each synthesized voice clip, and can revise an automatically generated script when the measured narration is materially too short or long. The final measured duration and any remaining difference are written to the job log.

Scripts use a deliberate **15% opening / 70% core idea / 15% resolution** structure. The opening starts with the subject rather than a generic preamble; the middle develops the idea; the ending resolves the setup with a complete final line.

Resume or tweak a job with `--job output/job_xxx`. `script.json` is reused when its requested duration is unchanged. Edit the narration or prompts there; changed narration is automatically re-synthesized. Delete an `mNNN.*` file to regenerate that scene's visual.

## Web studio

```bash
python webui.py
```

<<<<<<< HEAD
Open `http://127.0.0.1:5000`. The studio supports direct generation, idea queues, scheduling, script review, exports, and background music. Re-rendering a saved project remeasures narration and attempts to correct duration drift while reusing the saved visual assets (unless the script was manually edited). Tailwind CSS 4 is compiled into `static/app.css`, so the UI has no runtime CDN dependency. To rebuild the CSS after editing the UI:
=======
Open `http://127.0.0.1:5000`. The studio supports direct generation, idea queues, scheduling, script review, exports, and background music. Tailwind CSS 4 is compiled into `static/app.css`, so the UI has no runtime CDN dependency. To rebuild the CSS after editing the UI:
>>>>>>> 09673f3e04e823228dae1a54fa4c6fd47dcdebff

```bash
npm install
npm run build:css
```

<<<<<<< HEAD
## AI and visual-generation settings

Open **Admin panel → AI services** to change the Ollama API base URL and model tag, maximum output tokens, or the ComfyUI URL, timeout, and image/video generation megapixels. Longer scripts need more output tokens; the default is 8192, and the client retries with a larger budget if Ollama reports a length-truncated response. Duration revisions return narration only and reuse the existing visual prompts, reducing JSON output size. Use **Test connections** to check whether both services respond and whether the selected Ollama model is installed. Higher megapixel values need more GPU memory; the generation path keeps its lower-resolution fallback for memory errors. This project currently supports Ollama and ComfyUI; it does not store third-party API keys or configure OpenAI-compatible providers.

The default visual encodes use H.264 CRF 16 with the `slow` preset. This improves detail/bitrate efficiency at the cost of longer export time; edit `video.scene_crf`, `video.final_crf`, and `video.render_preset` in `config.yaml` if you prefer faster exports or smaller files.

## Background music

Drop `.mp3`, `.wav`, `.ogg`, `.m4a`, or `.flac` files into `music/` (or upload through the UI). A playable track is randomly selected for each export, normalized to a more audible level, faded in/out, gently ducked under narration, and mixed with a peak limiter. Set the custom **0–100%** level in **Settings → Music volume** (default 50%). The renderer skips files that do not contain an audio stream and logs when no usable track is found.

## Notes

- Edge TTS retries transient no-audio responses twice, validates the MP3/WAV files, and reports the voice/text length if synthesis still fails.
=======
## Background music

Drop `.mp3`, `.wav`, `.ogg`, `.m4a`, or `.flac` files into `music/` (or upload through the UI). A random track is mixed under the narration; volume is controlled in **Settings → Music volume**.

## Notes

>>>>>>> 09673f3e04e823228dae1a54fa4c6fd47dcdebff
- `ollama.think: false` is used for structured script responses; if an Ollama/model version still returns an empty or malformed response, the client retries once with thinking enabled and still validates the JSON before continuing.
- `video.narration_wpm` is the initial word-budget estimate. Actual voiceover duration is measured and the generated script is refined when needed.
- `video.duration_tolerance_s` and `video.duration_tolerance_ratio` control how much natural TTS timing variation is accepted without another script pass.
- Generated jobs are stored under `output/` (ignored by Git). Existing outputs are never regenerated unless the required scene asset is deleted or the job's target duration changes.
