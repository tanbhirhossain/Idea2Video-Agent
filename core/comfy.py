"""Resilient ComfyUI API client with workflow, node, and model diagnostics."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODEL_FIELDS = {"unet_name", "clip_name", "vae_name", "lora_name"}
H3_VIDEO_VAE_COMPATIBILITY = {
    "minimax_h3_video_vae_int8_convrot.safetensors": ("minimax_h3_video_vae_fp16.safetensors",),
    "minimax_h3_video_vae_fp16.safetensors": ("minimax_h3_video_vae_int8_convrot.safetensors",),
}


class ComfyClient:
    def __init__(self, host: str, timeout_s: int = 7200, poll_s: float = 2.0, log=None):
        self.host = host.rstrip("/")
        self.timeout = max(30, int(timeout_s))
        self.poll_s = max(0.5, float(poll_s))
        self.log = log
        self.client_id = str(uuid.uuid4())
        self._resolved_model_overrides = {}
        self._validated_workflow = None

    @staticmethod
    def _load(spec):
        path = Path(spec["workflow"])
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(f"ComfyUI workflow file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"ComfyUI workflow is not valid JSON: {path} ({exc})") from exc

    @staticmethod
    def _model_choices(object_info, class_type, field):
        node_info = object_info.get(class_type) or {}
        node_inputs = node_info.get("input") or {}
        available_inputs = {**node_inputs.get("required", {}), **node_inputs.get("optional", {})}
        entry = available_inputs.get(field)
        if not isinstance(entry, (list, tuple)) or not entry:
            return set()
        choices = entry[0]
        return {str(choice) for choice in choices} if isinstance(choices, (list, tuple, set)) else set()

    @staticmethod
    def _h3_video_vae_fallback(selected, available):
        basename = lambda name: str(name).replace("\\", "/").rsplit("/", 1)[-1].casefold()
        installed = {basename(name): str(name) for name in available}
        candidates = H3_VIDEO_VAE_COMPATIBILITY.get(basename(selected), ())
        for candidate in candidates:
            if basename(candidate) in installed:
                return installed[basename(candidate)]
        return None

    @staticmethod
    def _workflow_key(spec):
        path = Path(spec["workflow"])
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return str(path.resolve())

    def diagnostics(self, spec: dict) -> dict:
        """Check server reachability, API-node availability, mappings, and referenced models."""
        report = {
            "ok": False, "version": None, "errors": [], "warnings": [],
            "missing_models": [], "resolved_models": [], "available_h3_video_vaes": [],
        }
        try:
            response = requests.get(f"{self.host}/system_stats", timeout=8)
            response.raise_for_status()
            stats = response.json()
            system = stats.get("system", {}) if isinstance(stats, dict) else {}
            report["version"] = system.get("comfyui_version") or system.get("version")
        except Exception as exc:
            report["errors"].append(f"ComfyUI is not reachable at {self.host}: {exc}")
            return report

        try:
            workflow = self._load(spec)
        except (RuntimeError, KeyError, TypeError) as exc:
            report["errors"].append(str(exc))
            return report

        for key, mapping in (spec.get("map") or {}).items():
            try:
                node_id, field = mapping
                node = workflow.get(str(node_id))
                if not node:
                    report["errors"].append(f"Workflow mapping '{key}' points to missing node {node_id}.")
                elif field not in (node.get("inputs") or {}):
                    report["errors"].append(f"Workflow mapping '{key}' points to missing input {node_id}.{field}.")
            except (TypeError, ValueError):
                report["errors"].append(f"Workflow mapping '{key}' must contain a node ID and input name.")

        try:
            response = requests.get(f"{self.host}/object_info", timeout=25)
            response.raise_for_status()
            object_info = response.json()
            if not isinstance(object_info, dict):
                raise ValueError("ComfyUI returned an invalid /object_info response")
        except Exception as exc:
            report["errors"].append(f"Could not inspect ComfyUI nodes and model lists: {exc}")
            return report

        referenced_classes = sorted({str(node.get("class_type", "")) for node in workflow.values() if isinstance(node, dict)})
        missing_classes = [name for name in referenced_classes if name and name not in object_info]
        if missing_classes:
            report["errors"].append(
                "ComfyUI is missing workflow nodes: " + ", ".join(missing_classes)
                + ". Update ComfyUI and install/enable the required nodes, then restart it."
            )

        vae_choices = self._model_choices(object_info, "VAELoader", "vae_name")
        report["available_h3_video_vaes"] = sorted(
            name for name in vae_choices
            if str(name).replace("\\", "/").rsplit("/", 1)[-1].casefold().startswith("minimax_h3_video_vae")
        )
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type", ""))
            inputs = node.get("inputs") or {}
            for field in MODEL_FIELDS:
                selected = inputs.get(field)
                if not selected:
                    continue
                available = self._model_choices(object_info, class_type, field)
                if not available or str(selected) in available:
                    continue
                is_optional_lora = field == "lora_name" and not bool((spec.get("fixed") or {}).get("turbo", False))
                if class_type == "VAELoader" and field == "vae_name":
                    fallback = self._h3_video_vae_fallback(selected, available)
                    if fallback:
                        report["resolved_models"].append({
                            "node": str(node_id), "class": class_type, "field": field,
                            "configured": str(selected), "selected": fallback,
                        })
                        report["warnings"].append(
                            f"MiniMax H3 VAE compatibility: '{selected}' is not listed; "
                            f"this ComfyUI has '{fallback}', so generation will use the installed compatible VAE."
                        )
                        continue
                detail = {"node": str(node_id), "class": class_type, "field": field, "name": str(selected)}
                report["missing_models"].append(detail)
                message = f"{class_type}.{field} model '{selected}' is not listed by ComfyUI."
                if is_optional_lora:
                    report["warnings"].append(message + " It is only needed if turbo mode is enabled.")
                elif class_type == "VAELoader" and field == "vae_name" and str(selected).startswith("minimax_h3_video_vae"):
                    available_names = ", ".join(report["available_h3_video_vaes"]) or "none found"
                    report["errors"].append(
                        f"{message} Available MiniMax H3 video VAE files: {available_names}. "
                        "Install either minimax_h3_video_vae_int8_convrot.safetensors or "
                        "minimax_h3_video_vae_fp16.safetensors in ComfyUI/models/vae/, "
                        "then restart ComfyUI so it refreshes the VAE list."
                    )
                else:
                    report["errors"].append(message)

        report["ok"] = not report["errors"]
        return report

    def _apply_resolved_models(self, spec, workflow):
        if self._validated_workflow != self._workflow_key(spec):
            return workflow
        for node_id, fields in self._resolved_model_overrides.items():
            node = workflow.get(str(node_id)) or {}
            inputs = node.get("inputs") or {}
            for field, selected in fields.items():
                if field in inputs:
                    inputs[field] = selected
        return workflow

    def validate(self, spec: dict):
        report = self.diagnostics(spec)
        self._validated_workflow = self._workflow_key(spec)
        self._resolved_model_overrides = {}
        for item in report.get("resolved_models", []):
            self._resolved_model_overrides.setdefault(str(item["node"]), {})[str(item["field"])] = item["selected"]
        if not report["ok"]:
            details = " ".join(report["errors"])
            raise RuntimeError(f"ComfyUI workflow check failed: {details}")
        return report

    def combo_options(self, spec, key):
        node, field = spec["map"][key]
        cls = self._load(spec)[str(node)]["class_type"]
        response = requests.get(f"{self.host}/object_info/{cls}", timeout=10)
        response.raise_for_status()
        info = response.json()[cls]["input"]
        entry = {**info.get("required", {}), **info.get("optional", {})}[field]
        return entry[0] if isinstance(entry[0], list) else entry[1].get("options", [])

    def resolve_aspect(self, spec, aspect: str):
        """Map an aspect ratio to the exact ResolutionSelector option ComfyUI accepts."""
        if "aspect" not in spec.get("map", {}):
            return None
        try:
            return next((option for option in self.combo_options(spec, "aspect")
                         if str(option).startswith(aspect)), None)
        except (KeyError, IndexError, TypeError, requests.RequestException):
            return spec.get("aspect_fallback", {}).get(aspect, aspect)

    def generate(self, spec: dict, values: dict, out_stem: Path) -> Path:
        return self._generate_once(spec, values, out_stem)

    def _emit(self, message):
        if callable(self.log):
            try:
                self.log(str(message))
            except Exception:
                pass

    @staticmethod
    def _prompt_state_path(out_stem):
        stem = Path(out_stem)
        return stem.parent / ".comfy_prompts" / f"{stem.name}.json"

    @staticmethod
    def _prompt_signature(workflow, spec):
        payload = json.loads(json.dumps(workflow, ensure_ascii=False))
        seed_mapping = (spec.get("map") or {}).get("seed")
        if seed_mapping:
            node_id, field = seed_mapping
            if str(node_id) in payload and field in (payload[str(node_id)].get("inputs") or {}):
                payload[str(node_id)]["inputs"][field] = "<random-seed>"
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _load_prompt_state(state_path):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            return state if isinstance(state, dict) and state.get("prompt_id") else None
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _save_prompt_state(state_path, state):
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(state_path)

    def _queue_state(self, prompt_id):
        """Return ComfyUI's current queue position for a prompt, if available."""
        try:
            response = requests.get(f"{self.host}/queue", timeout=10)
            response.raise_for_status()
            queue = response.json()
            if not isinstance(queue, dict):
                return "unknown (invalid /queue response)"
            for key, label in (("queue_running", "running"), ("queue_pending", "queued")):
                entries = queue.get(key) or []
                for entry in entries:
                    candidate = None
                    if isinstance(entry, dict):
                        candidate = entry.get("prompt_id") or entry.get("id")
                    elif isinstance(entry, (list, tuple)):
                        candidate = entry[1] if len(entry) > 1 else (entry[0] if entry else None)
                    if str(candidate) == str(prompt_id):
                        return label
            return "not listed in ComfyUI queue"
        except Exception as exc:
            return f"queue status unavailable ({exc})"

    def _prompt_is_known(self, prompt_id):
        """Avoid retrying a saved prompt that ComfyUI has already forgotten."""
        try:
            response = requests.get(f"{self.host}/history/{prompt_id}", timeout=10)
            response.raise_for_status()
            history = response.json()
            if isinstance(history, dict) and history.get(prompt_id):
                return True
        except Exception:
            return True  # an unavailable status endpoint is not proof the prompt vanished
        queue_state = self._queue_state(prompt_id)
        if queue_state in ("running", "queued"):
            return True
        return queue_state != "not listed in ComfyUI queue"

    def _wait_for_prompt(self, prompt_id, wait_s=30):
        deadline = time.monotonic() + max(0, wait_s)
        while time.monotonic() < deadline:
            if self._prompt_is_known(prompt_id):
                return True
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        return self._prompt_is_known(prompt_id)

    def _generate_once(self, spec: dict, values: dict, out_stem: Path) -> Path:
        workflow = self._apply_resolved_models(spec, self._load(spec))
        values = {**(spec.get("fixed") or {}), **{key: value for key, value in values.items() if value is not None}}
        for key, value in values.items():
            if key not in spec.get("map", {}):
                continue
            node_id, field = spec["map"][key]
            node_id = str(node_id)
            if node_id not in workflow or field not in (workflow[node_id].get("inputs") or {}):
                raise RuntimeError(f"ComfyUI workflow mapping '{key}' is invalid ({node_id}.{field}).")
            workflow[node_id]["inputs"][field] = value

        signature = self._prompt_signature(workflow, spec)
        state_path = self._prompt_state_path(out_stem)
        previous = self._load_prompt_state(state_path)
        signature_matches = bool(previous and previous.get("signature") == signature)
        resumed = False
        prompt_id = None
        if signature_matches:
            prompt_id = str(previous["prompt_id"])
            if self._prompt_is_known(prompt_id):
                resumed = True
                self._emit(f"ComfyUI: resuming saved prompt {prompt_id}; it will not be submitted a second time")
            elif previous.get("status") in ("submitting", "ack_timeout"):
                self._emit(f"ComfyUI: checking whether previous queue request {prompt_id} was accepted")
                if self._wait_for_prompt(prompt_id, wait_s=60):
                    resumed = True
                    self._emit(f"ComfyUI: recovered saved prompt {prompt_id}; it will not be submitted again")
                else:
                    self._emit(
                        f"ComfyUI warning: prompt {prompt_id} is still absent from queue/history after 60s; "
                        "retrying with the same ID"
                    )
            else:
                self._emit(f"ComfyUI warning: saved prompt {prompt_id} is no longer in history or queue; submitting this scene again")
                state_path.unlink(missing_ok=True)
                prompt_id = None
        elif previous:
            self._emit(
                f"ComfyUI warning: saved prompt {previous.get('prompt_id')} belongs to different scene inputs; "
                "leaving it untouched and submitting the updated scene"
            )

        if not resumed:
            prompt_id = prompt_id or str(uuid.uuid4())
            prompt_state = {
                "prompt_id": prompt_id, "signature": signature,
                "workflow": self._workflow_key(spec), "created_at": time.time(),
                "status": "submitting", "client_id": self.client_id,
            }
            try:
                # Persist the caller-chosen prompt_id before POST. ComfyUI accepts
                # custom prompt IDs, allowing a timed-out HTTP acknowledgement to
                # be checked safely instead of blindly queuing a duplicate.
                self._save_prompt_state(state_path, prompt_state)
            except OSError as exc:
                raise RuntimeError(f"Could not save ComfyUI prompt recovery state; refusing to queue an untracked prompt: {exc}") from exc
            payload = {
                "prompt": workflow,
                "prompt_id": prompt_id,
                "client_id": self.client_id,
                "extra_data": {"idea2video_signature": signature},
            }
            try:
                response = requests.post(
                    f"{self.host}/prompt", json=payload, timeout=(10, 300),
                )
                response.raise_for_status()
                result = response.json()
                returned_id = str(result["prompt_id"])
                if returned_id != prompt_id:
                    prompt_id = returned_id
                    prompt_state["prompt_id"] = prompt_id
            except (requests.Timeout, requests.ConnectionError) as exc:
                prompt_state["status"] = "ack_timeout"
                try:
                    self._save_prompt_state(state_path, prompt_state)
                except OSError:
                    pass
                if self._wait_for_prompt(prompt_id, wait_s=60):
                    prompt_state["status"] = "queued"
                    self._save_prompt_state(state_path, prompt_state)
                    self._emit(f"ComfyUI: recovered prompt {prompt_id} after the queue acknowledgement timed out")
                else:
                    raise TimeoutError(
                        f"ComfyUI did not acknowledge POST /prompt within 300s. "
                        f"Prompt ID {prompt_id} is saved and no second prompt was submitted. Check ComfyUI's queue; "
                        "retrying will check/resume this same ID."
                    ) from exc
            except requests.RequestException as exc:
                state_path.unlink(missing_ok=True)
                detail = getattr(exc.response, "text", "") if getattr(exc, "response", None) is not None else ""
                raise RuntimeError(f"Could not queue the ComfyUI workflow at {self.host}: {detail or exc}") from exc
            except (KeyError, ValueError) as exc:
                state_path.unlink(missing_ok=True)
                raise RuntimeError("ComfyUI did not return a prompt ID for the queued workflow.") from exc
            prompt_state["status"] = "queued"
            try:
                self._save_prompt_state(state_path, prompt_state)
            except OSError as exc:
                self._emit(f"ComfyUI warning: prompt {prompt_id} was queued but its recovery status could not be updated: {exc}")
            self._emit(f"ComfyUI: queued prompt {prompt_id}")

        started = time.monotonic()
        last_heartbeat = started
        last_error = None
        queue_state = "not checked yet"
        media_type = "video" if "max_clip_s" in spec else "image"
        while time.monotonic() - started < self.timeout:
            try:
                response = requests.get(f"{self.host}/history/{prompt_id}", timeout=20)
                response.raise_for_status()
                history = response.json()
                last_error = None
            except requests.RequestException as exc:
                last_error = exc
                history = {}
            entry = history.get(prompt_id) if isinstance(history, dict) else None
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error" or status.get("completed") is False and status.get("messages"):
                    state_path.unlink(missing_ok=True)
                    raise RuntimeError(self._execution_error(entry))
                output = self._pick_output(entry.get("outputs", {}), media_type=media_type)
                if output:
                    result = self._download(output, Path(out_stem))
                    state_path.unlink(missing_ok=True)
                    return result
                if status.get("completed") is True:
                    state_path.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"ComfyUI completed prompt {prompt_id} but returned no {media_type} file. "
                        "Check the workflow's SaveVideo/SaveImage node and ComfyUI output log."
                    )

            now = time.monotonic()
            if now - last_heartbeat >= 30:
                queue_state = self._queue_state(prompt_id)
                elapsed = int(now - started)
                self._emit(
                    f"ComfyUI: prompt {prompt_id} is {queue_state} after {elapsed // 60}m {elapsed % 60}s "
                    f"(timeout {self.timeout // 60}m); waiting for its output"
                )
                last_heartbeat = now
            time.sleep(self.poll_s)

        queue_state = self._queue_state(prompt_id)
        elapsed = int(time.monotonic() - started)
        message = (
            f"ComfyUI prompt {prompt_id} did not return a {media_type} within {self.timeout}s "
            f"({elapsed // 60}m {elapsed % 60}s). Last queue state: {queue_state}. "
            f"Recovery state is saved at {state_path}; retrying this scene will resume the same prompt, not duplicate it."
        )
        if last_error:
            message += f" Last history request error: {last_error}."
        raise TimeoutError(message)

    @staticmethod
    def _execution_error(entry: dict) -> str:
        status = entry.get("status") or {}
        messages = status.get("messages") or status.get("status_messages") or []
        for message in messages:
            if not isinstance(message, (list, tuple)) or len(message) < 2:
                continue
            detail = message[1]
            if not isinstance(detail, dict):
                continue
            text = detail.get("exception_message") or detail.get("message")
            node_type = detail.get("node_type") or detail.get("class_type") or "unknown node"
            node_id = detail.get("node_id") or "?"
            if text:
                return f"ComfyUI failed in {node_type} (node {node_id}): {text}"
        return f"ComfyUI workflow execution failed: {status}"

    @staticmethod
    def _pick_output(outputs: dict, media_type: str = "image"):
        found = []
        allowed = {".mp4", ".webm", ".mkv"} if media_type == "video" else {".png", ".jpg", ".jpeg", ".webp"}
        for node_output in outputs.values():
            if not isinstance(node_output, dict):
                continue
            for items in node_output.values():
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict) or not item.get("filename"):
                            continue
                        if Path(item["filename"]).suffix.lower() in allowed:
                            found.append(item)
        found.sort(key=lambda item: item.get("type") != "output")
        return found[0] if found else None

    def _download(self, file_info: dict, out_stem: Path) -> Path:
        try:
            response = requests.get(
                f"{self.host}/view",
                params={"filename": file_info["filename"],
                        "subfolder": file_info.get("subfolder", ""),
                        "type": file_info.get("type", "output")},
                timeout=max(60, min(self.timeout, 300)),
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"ComfyUI created an output but it could not be downloaded: {exc}") from exc
        if not response.content:
            raise RuntimeError(f"ComfyUI returned an empty output file: {file_info.get('filename', 'unknown')}")
        out_stem.parent.mkdir(parents=True, exist_ok=True)
        suffix = Path(file_info["filename"]).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".mkv"}:
            raise RuntimeError(f"ComfyUI returned an unsupported media format: {suffix or 'unknown'}")
        out_path = out_stem.with_suffix(suffix)
        temporary = out_path.with_name(out_path.stem + ".partial" + suffix)
        temporary.write_bytes(response.content)
        if temporary.stat().st_size < 64:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"ComfyUI output is incomplete: {file_info.get('filename', 'unknown')}")
        temporary.replace(out_path)
        return out_path
