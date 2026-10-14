# Idea → Video Studio

Turn an idea, article, or RSS item into a narrated video using Ollama, ComfyUI, Microsoft Edge TTS (with Google TTS fallback), and FFmpeg.

## Setup

1. Install Python dependencies: `python -m pip install -r requirements.txt`. The Windows launcher also checks for the Google TTS fallback and installs project requirements if it is missing.
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

Open `http://127.0.0.1:5000`. Single-create and queued generations are saved transactionally in `output/ideas.sqlite3`, continue in the background through page reloads, show live stage/progress, and are requeued from saved assets after an unclean server restart. Existing `output/ideas.json` queues are imported once automatically. The local worker is deliberately sequential to protect the ComfyUI GPU. The dashboard preserves active video elements during refresh, and MP4 delivery supports HTTP byte ranges. Re-rendering a saved project remeasures narration and attempts to correct duration drift while reusing the saved visual assets (unless the script was manually edited). Finished videos get YouTube and Facebook metadata in `seo.json`. Tailwind CSS 4 is compiled into `static/app.css`, so the UI has no runtime CDN dependency. To rebuild the CSS after editing the UI:

```bash
npm install
npm run build:css
```

## AI and visual-generation settings

Open **Admin panel → AI services** to change the Ollama API base URL and model tag, maximum output tokens, or the ComfyUI URL, timeout, and image/video generation megapixels. Longer scripts need more output tokens; the default is 8192, and the client retries with a larger budget if Ollama reports a length-truncated response. Duration revisions return narration only and reuse the existing visual prompts, reducing JSON output size. Use **Test connections** to check whether both services respond and whether the selected Ollama model is installed. Higher megapixel values need more GPU memory; the generation path keeps its lower-resolution fallback for memory errors. This project currently supports Ollama and ComfyUI; it does not store third-party API keys or configure OpenAI-compatible providers.

The default visual encodes use H.264 CRF 16 with the `slow` preset. This improves detail/bitrate efficiency at the cost of longer export time; edit `video.scene_crf`, `video.final_crf`, and `video.render_preset` in `config.yaml` if you prefer faster exports or smaller files.

## English and Bangla publishing

Choose **English** or **Bangla** in either Create Video or Idea Queue. Bangla defaults to Microsoft's `bn-BD-NabanitaNeural` Edge voice at a slightly slower `-5%` rate for clearer diction; choose the male `bn-BD-PradeepNeural` voice or enter another Edge voice in **Studio Settings**. Bangla has its own draft pace estimate, and the pipeline still measures synthesized speech and refines the script to fit the requested duration. Bangla script generation now explicitly asks for natural, spoken Bangladeshi Bengali rather than literal/bookish phrasing. Captions default to the narration language; select the other language to generate concise, aligned translations with Ollama. Bangla subtitles are word-wrapped into short two-line cues and use `Nirmala UI` by default; change the installed font in **Studio Settings** if needed.

Video mode uses the native ComfyUI MiniMax H3 workflow. Keep ComfyUI updated with the [official H3 workflow requirements](https://docs.comfy.org/tutorials/video/minimax/minimax-h3-native), install the model files listed there, and use **Admin panel → Diagnose AI video** to check node availability, workflow mappings, and model names. The workflow prefers `minimax_h3_video_vae_int8_convrot.safetensors`; if the ComfyUI model list instead contains the compatible H3 `minimax_h3_video_vae_fp16.safetensors`, the client automatically selects it for that run. If neither appears, download one of those H3 video VAEs into `ComfyUI/models/vae/` and restart ComfyUI so it refreshes its model list. The diagnostic reports the detected VAE and any compatibility selection before generation starts. H3 prompts now default to a 7200-second (2-hour) per-prompt timeout and write a ComfyUI queue heartbeat to the job log every 30 seconds. Prompt IDs are saved under each job's `.comfy_prompts/` folder; retrying after an app timeout resumes the same prompt instead of submitting a duplicate. The Admin panel also offers the optional 8-step turbo LoRA for faster generation when its model file is installed; it makes a small quality trade-off and is checked during preflight.

## Background music

Drop `.mp3`, `.wav`, `.ogg`, `.m4a`, or `.flac` files into `music/` (or upload through the UI). A playable track is randomly selected for each export, normalized to a more audible level, faded in/out, gently ducked under narration, and mixed with a peak limiter. Set the custom **0–100%** level in **Settings → Music volume** (default 50%). The renderer skips files that do not contain an audio stream and logs when no usable track is found.

## Notes

- Microsoft Edge TTS remains the primary speech provider. Transient failures are retried; an Edge 403 switches immediately to Google TTS for English or Bangla so the job can continue. The Google fallback sends that scene's narration to Google, uses Google's voice (not the selected Edge voice/gender), and approximates configured speaking rate with FFmpeg. The job log and `aNNN.meta.json` record when fallback audio was used. If both providers fail, the error includes each provider's cause without exposing signed request URLs.
- `ollama.think: false` is used for structured script responses; if an Ollama/model version still returns an empty or malformed response, the client retries once with thinking enabled and still validates the JSON before continuing.
- `video.narration_wpm` is the initial word-budget estimate. Actual voiceover duration is measured and the generated script is refined when needed.
- `video.duration_tolerance_s` and `video.duration_tolerance_ratio` control how much natural TTS timing variation is accepted without another script pass.
- Generated jobs are stored under `output/` (ignored by Git). Existing outputs are never regenerated unless the required scene asset is deleted or the job's target duration changes.
