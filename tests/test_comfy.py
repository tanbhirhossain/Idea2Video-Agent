import json
import tempfile
import unittest

import requests
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.comfy import ComfyClient


class ComfyClientTests(unittest.TestCase):
    def test_video_diagnostics_identify_missing_required_model_and_node(self):
        with tempfile.TemporaryDirectory() as temporary:
            workflow_path = Path(temporary) / "workflow.json"
            workflow_path.write_text(json.dumps({
                "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "h3-model.safetensors"}},
                "2": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"prompt": "test"}},
            }), encoding="utf-8")
            spec = {"workflow": str(workflow_path), "map": {"prompt": ["2", "prompt"]}}
            stats = MagicMock()
            stats.json.return_value = {"system": {"comfyui_version": "0.34.0"}}
            info = MagicMock()
            info.json.return_value = {
                "UNETLoader": {"input": {"required": {"unet_name": [["other-model.safetensors"], {}]}}},
                # Mimic an older ComfyUI that has no native MiniMax H3 class.
            }
            with patch("core.comfy.requests.get", side_effect=[stats, info]):
                report = ComfyClient("http://comfy.test").diagnostics(spec)
        self.assertFalse(report["ok"])
        self.assertEqual(report["version"], "0.34.0")
        self.assertTrue(any("MiniMaxH3ImageToVideo" in error for error in report["errors"]))
        self.assertTrue(any("h3-model.safetensors" in error for error in report["errors"]))

    def test_video_vae_automatically_uses_installed_fp16_compatibility_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            workflow_path = Path(temporary) / "workflow.json"
            workflow = {"vae": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": "minimax_h3_video_vae_int8_convrot.safetensors"},
            }}
            workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
            spec = {"workflow": str(workflow_path), "map": {}}
            stats = MagicMock()
            stats.json.return_value = {"system": {"comfyui_version": "0.35.0"}}
            info = MagicMock()
            info.json.return_value = {
                "VAELoader": {"input": {"required": {
                    "vae_name": [["minimax_h3_video_vae_fp16.safetensors"], {}],
                }}}
            }
            client = ComfyClient("http://comfy.test")
            with patch("core.comfy.requests.get", side_effect=[stats, info]):
                report = client.validate(spec)
            adjusted = client._apply_resolved_models(spec, client._load(spec))
        self.assertTrue(report["ok"])
        self.assertEqual(report["available_h3_video_vaes"], ["minimax_h3_video_vae_fp16.safetensors"])
        self.assertEqual(report["resolved_models"][0]["selected"], "minimax_h3_video_vae_fp16.safetensors")
        self.assertEqual(adjusted["vae"]["inputs"]["vae_name"], "minimax_h3_video_vae_fp16.safetensors")
        self.assertTrue(any("generation will use" in warning for warning in report["warnings"]))

    def test_missing_h3_vae_error_gives_model_folder_and_restart_steps(self):
        with tempfile.TemporaryDirectory() as temporary:
            workflow_path = Path(temporary) / "workflow.json"
            workflow_path.write_text(json.dumps({"vae": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": "minimax_h3_video_vae_int8_convrot.safetensors"},
            }}), encoding="utf-8")
            spec = {"workflow": str(workflow_path), "map": {}}
            stats = MagicMock()
            stats.json.return_value = {"system": {"comfyui_version": "0.35.0"}}
            info = MagicMock()
            info.json.return_value = {"VAELoader": {"input": {"required": {
                "vae_name": [["other_vae.safetensors"], {}],
            }}}}
            with patch("core.comfy.requests.get", side_effect=[stats, info]):
                report = ComfyClient("http://comfy.test").diagnostics(spec)
        self.assertFalse(report["ok"])
        self.assertEqual(report["available_h3_video_vaes"], [])
        error = " ".join(report["errors"])
        self.assertIn("ComfyUI/models/vae/", error)
        self.assertIn("restart ComfyUI", error)
        self.assertIn("minimax_h3_video_vae_fp16.safetensors", error)

    def test_queue_post_read_timeout_recovers_the_custom_prompt_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow_path = root / "workflow.json"
            workflow_path.write_text(json.dumps({"1": {"class_type": "SaveImage", "inputs": {}}}), encoding="utf-8")
            spec = {"workflow": str(workflow_path), "map": {}}
            client = ComfyClient("http://comfy.test")
            client.timeout = 0
            request_data = {}

            def timeout_post(url, json, timeout):
                request_data.update(url=url, payload=json, timeout=timeout)
                raise requests.ReadTimeout("simulated slow queue acknowledgement")

            with patch("core.comfy.requests.post", side_effect=timeout_post) as post, \
                 patch.object(client, "_wait_for_prompt", return_value=True) as wait_for_prompt, \
                 patch.object(client, "_queue_state", return_value="running"):
                with self.assertRaises(TimeoutError):
                    client.generate(spec, {}, root / "job" / "m000")

            prompt_id = request_data["payload"]["prompt_id"]
            state = client._load_prompt_state(client._prompt_state_path(root / "job" / "m000"))
            self.assertEqual(request_data["timeout"], (10, 300))
            self.assertEqual(state["prompt_id"], prompt_id)
            self.assertEqual(state["status"], "queued")
            self.assertEqual(request_data["payload"]["extra_data"]["idea2video_signature"], state["signature"])
            wait_for_prompt.assert_called_once_with(prompt_id, wait_s=60)
            post.assert_called_once()

    def test_ambiguous_submission_retry_checks_status_and_reuses_the_saved_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow_path = root / "workflow.json"
            workflow_path.write_text(json.dumps({"1": {"class_type": "SaveImage", "inputs": {}}}), encoding="utf-8")
            spec = {"workflow": str(workflow_path), "map": {}}
            stem = root / "job" / "m000"
            first = ComfyClient("http://comfy.test")
            first.timeout = 0
            with patch("core.comfy.requests.post", side_effect=requests.ReadTimeout("ack missing")), \
                 patch.object(first, "_wait_for_prompt", return_value=False):
                with self.assertRaisesRegex(TimeoutError, "no second prompt was submitted"):
                    first.generate(spec, {}, stem)
            saved_before_retry = first._load_prompt_state(first._prompt_state_path(stem))
            self.assertEqual(saved_before_retry["status"], "ack_timeout")

            second = ComfyClient("http://comfy.test")
            second.timeout = 0
            response = MagicMock()
            response.json.return_value = {"prompt_id": saved_before_retry["prompt_id"]}
            with patch("core.comfy.requests.post", return_value=response) as post, \
                 patch.object(second, "_prompt_is_known", return_value=False), \
                 patch.object(second, "_wait_for_prompt", return_value=False) as wait_for_prompt, \
                 patch.object(second, "_queue_state", return_value="running"):
                with self.assertRaises(TimeoutError):
                    second.generate(spec, {}, stem)

            self.assertEqual(post.call_args.kwargs["json"]["prompt_id"], saved_before_retry["prompt_id"])
            wait_for_prompt.assert_called_once_with(saved_before_retry["prompt_id"], wait_s=60)

    def test_timed_out_prompt_is_persisted_and_resumed_without_duplicate_submission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow_path = root / "workflow.json"
            workflow = {"1": {"class_type": "SaveImage", "inputs": {}}}
            workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
            spec = {"workflow": str(workflow_path), "map": {}}
            stem = root / "job" / "m000"
            first = ComfyClient("http://comfy.test")
            first.timeout = 0
            post_response = MagicMock()
            post_response.json.return_value = {"prompt_id": "prompt-resume-1"}
            with patch("core.comfy.requests.post", return_value=post_response) as post, \
                 patch.object(first, "_queue_state", return_value="running"):
                with self.assertRaises(TimeoutError) as caught:
                    first.generate(spec, {}, stem)
            self.assertIn("Last queue state: running", str(caught.exception))
            state_path = first._prompt_state_path(stem)
            self.assertTrue(state_path.is_file())

            history_response = MagicMock()
            history_response.json.return_value = {"prompt-resume-1": {
                "status": {"status_str": "success", "completed": True},
                "outputs": {"1": {"images": [{"filename": "recovered.png", "type": "output"}]}},
            }}
            media_response = MagicMock()
            media_response.content = b"x" * 128
            logs = []
            resumed = ComfyClient("http://comfy.test", log=logs.append)
            with patch("core.comfy.requests.post") as duplicate_post, \
                 patch("core.comfy.requests.get", side_effect=[history_response, history_response, media_response]):
                result = resumed.generate(spec, {}, stem)
            duplicate_post.assert_not_called()
        self.assertEqual(result.name, "m000.png")
        self.assertFalse(state_path.exists())
        self.assertTrue(any("resuming saved prompt" in line for line in logs))

    def test_execution_error_includes_failing_node_and_message(self):
        entry = {"status": {"status_str": "error", "messages": [[
            "execution_error", {"node_type": "MiniMaxH3ImageToVideo", "node_id": "140:131",
                                "exception_message": "Required model was not found"},
        ]]}}
        error = ComfyClient._execution_error(entry)
        self.assertIn("MiniMaxH3ImageToVideo", error)
        self.assertIn("node 140:131", error)
        self.assertIn("Required model was not found", error)

    def test_output_picker_selects_the_expected_media_kind(self):
        outputs = {"1": {"images": [{"filename": "preview.png", "type": "output"}]},
                   "2": {"gifs": [{"filename": "result.mp4", "type": "output"}]}}
        self.assertEqual(ComfyClient._pick_output(outputs, "video")["filename"], "result.mp4")
        self.assertEqual(ComfyClient._pick_output(outputs, "image")["filename"], "preview.png")

    def test_saved_h3_workflow_uses_current_video_vae_and_resolution_cap(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
        workflow = json.loads((root / "workflows" / "video_minimax_h3_t2v.json").read_text(encoding="utf-8"))
        self.assertEqual(workflow["140:119"]["inputs"]["vae_name"], "minimax_h3_video_vae_int8_convrot.safetensors")
        self.assertEqual(config["comfy"]["video"]["fixed"]["megapixels"], 1.0)
        self.assertEqual(config["comfy"]["video"]["min_clip_s"], 5)


if __name__ == "__main__":
    unittest.main()
