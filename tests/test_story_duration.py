import json
import unittest
from unittest.mock import MagicMock, patch

from core.script_gen import (
    _call_ollama,
    _decode_json_content,
    _merge_revised_narration,
    make_script,
    revise_script_for_duration,
    script_content_hash,
    script_was_edited,
    story_plan,
)
from core.timing import (
    duration_tolerance,
    final_duration_from_narration,
    narration_target_seconds,
)


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.video = {
            "transition_s": 0.5,
            "end_screen": {"enabled": True, "channel": "Truth Unboxed", "seconds": 4},
            "duration_tolerance_s": 2,
            "duration_tolerance_ratio": 0.04,
        }

    def test_65_second_target_reserves_transition_and_outro(self):
        narration = narration_target_seconds(self.video, 65)
        self.assertAlmostEqual(narration, 60.5)
        self.assertAlmostEqual(final_duration_from_narration(self.video, narration), 65)

    def test_disabled_outro_only_reserves_transition(self):
        video = {**self.video, "end_screen": {"enabled": False, "channel": "Truth Unboxed", "seconds": 4}}
        self.assertAlmostEqual(narration_target_seconds(video, 65), 64.5)

    def test_too_short_duration_is_rejected_instead_of_silently_overshooting(self):
        with self.assertRaisesRegex(ValueError, "leaves only"):
            narration_target_seconds(self.video, 5)

    def test_large_tts_drift_is_outside_tolerance(self):
        target = narration_target_seconds(self.video, 65)
        tolerance = duration_tolerance(self.video, target)
        self.assertGreater(tolerance, 2.0)
        self.assertGreater(15.5, tolerance)
        self.assertLessEqual(1.0, tolerance)


class StoryPlanTests(unittest.TestCase):
    def test_plan_uses_runtime_and_15_70_15_word_budget(self):
        plan = story_plan(60.5, scene_seconds=6, words_per_minute=190)
        self.assertEqual(plan["scene_count"], 11)
        self.assertEqual(plan["total_words"], 192)
        self.assertEqual(
            plan["opening_words"] + plan["core_words"] + plan["ending_words"],
            plan["total_words"],
        )
        self.assertAlmostEqual(plan["opening_words"] / plan["total_words"], 0.15, delta=0.01)
        self.assertAlmostEqual(plan["core_words"] / plan["total_words"], 0.70, delta=0.01)
        self.assertAlmostEqual(plan["ending_words"] / plan["total_words"], 0.15, delta=0.01)

    @patch("core.script_gen._call_ollama")
    def test_generated_script_records_arc_and_target_duration(self, call_model):
        call_model.return_value = {
            "title": "A complete story",
            "scenes": [
                {"narration": "The story begins here.", "visual_prompt": "A dawn-lit street."},
                {"narration": "The story resolves here.", "visual_prompt": "A calm street at sunrise."},
            ],
        }
        config = {
            "video": {"scene_seconds": 6, "narration_wpm": 190},
            "ollama": {"host": "http://localhost:11434", "model": "test"},
        }
        script = make_script(config, "A small idea", "image", "shorts", 65,
                             "English", narration_seconds=60.5)
        metadata = script["_generator"]
        prompt = call_model.call_args.args[1]
        self.assertEqual(metadata["requested_seconds"], 65.0)
        self.assertEqual(metadata["arc_percent"], {"opening": 15, "core": 70, "resolution": 15})
        self.assertIn("first 15 percent", prompt)
        self.assertIn("next 70 percent", prompt)
        self.assertIn("final 15 percent", prompt)
        self.assertIn("do not add greetings", prompt.lower())

    @patch("core.script_gen._call_ollama")
    def test_duration_revision_sends_narration_only_and_preserves_visual_prompts(self, call_model):
        call_model.side_effect = [
            {"title": "Expanded story",
             "scenes": [{"narration": "Expanded opening with helpful context."},
                        {"narration": "A clear ending that resolves the story."}]},
            {"title": "Expanded story",
             "scenes": [{"narration": "Opening expands."}, {"narration": "Ending resolves."}]},
        ]
        current = {
            "title": "Short story",
            "scenes": [
                {"narration": "Opening.", "visual_prompt": "A sunrise over a city."},
                {"narration": "Ending.", "visual_prompt": "A quiet city at dusk."},
            ],
        }
        config = {"video": {"scene_seconds": 6, "narration_wpm": 190},
                  "ollama": {"host": "http://localhost:11434", "model": "test"}}
        revised = revise_script_for_duration(config, "A city story", "image", "shorts",
                                             15, 10, 5, "English", current, log=lambda _: None)
        self.assertEqual(len(revised["scenes"]), 2)
        self.assertEqual(revised["scenes"][0]["visual_prompt"], "A sunrise over a city.")
        self.assertEqual(revised["scenes"][1]["visual_prompt"], "A quiet city at dusk.")
        self.assertEqual(call_model.call_count, 2)
        prompt = call_model.call_args_list[0].args[1]
        self.assertIn("too short by about 5.0 seconds", prompt)
        self.assertIn("narration only", prompt)
        self.assertIn('"narration":"Opening."', prompt)
        self.assertNotIn('"visual_prompt":"A sunrise over a city."', prompt)

    def test_revision_scene_count_mismatch_is_reconciled_with_saved_visuals(self):
        current = {
            "title": "Story",
            "scenes": [
                {"narration": "Old scene one.", "visual_prompt": "Visual one."},
                {"narration": "Old scene two.", "visual_prompt": "Visual two."},
                {"narration": "Old scene three.", "visual_prompt": "Visual three."},
            ],
        }
        logs = []
        revised = _merge_revised_narration({
            "title": "Story",
            "scenes": [{"narration": "Opening begins. The main events follow."},
                       {"narration": "The ending resolves."}],
        }, current, log=logs.append)
        self.assertEqual(len(revised["scenes"]), 3)
        self.assertEqual([scene["visual_prompt"] for scene in revised["scenes"]],
                         ["Visual one.", "Visual two.", "Visual three."])
        self.assertTrue(any("returned 2 scenes instead of 3; split narration" in line for line in logs))

    def test_extra_revision_scenes_are_merged_without_losing_text(self):
        current = {
            "title": "Story",
            "scenes": [
                {"narration": "Old scene one.", "visual_prompt": "Visual one."},
                {"narration": "Old scene two.", "visual_prompt": "Visual two."},
            ],
        }
        revised = _merge_revised_narration({
            "title": "Story",
            "scenes": [{"narration": "First beat."}, {"narration": "Middle beat."},
                       {"narration": "Final beat."}],
        }, current)
        self.assertEqual(len(revised["scenes"]), 2)
        combined = " ".join(scene["narration"] for scene in revised["scenes"])
        self.assertIn("First beat.", combined)
        self.assertIn("Middle beat.", combined)
        self.assertIn("Final beat.", combined)

    def test_script_edit_detection_ignores_private_metadata(self):
        script = {
            "title": "Title",
            "scenes": [{"narration": "Original narration.", "visual_prompt": "A scene."}],
        }
        script["_generator"] = {"content_hash": script_content_hash(script)}
        self.assertFalse(script_was_edited(script))
        script["scenes"][0]["narration"] = "Edited narration."
        self.assertTrue(script_was_edited(script))


class OllamaResponseTests(unittest.TestCase):
    def test_structured_request_disables_thinking_by_default(self):
        payload = {"title": "T", "scenes": []}
        response = MagicMock()
        response.json.return_value = {"message": {"content": json.dumps(payload)}}
        config = {"ollama": {"host": "http://localhost:11434", "model": "qwen3.5:latest"}}
        with patch("core.script_gen.requests.post", return_value=response) as post:
            result = _call_ollama(config, "return JSON")
        self.assertEqual(result, payload)
        self.assertIs(post.call_args.kwargs["json"]["think"], False)
        self.assertEqual(post.call_args.kwargs["json"]["format"], "json")
        self.assertEqual(post.call_args.kwargs["json"]["options"]["num_predict"], 8192)

    def test_length_truncation_retries_with_a_larger_budget_without_enabling_thinking(self):
        truncated = MagicMock()
        truncated.json.return_value = {
            "message": {"content": '{"title":"cut off'},
            "done_reason": "length",
        }
        complete = MagicMock()
        complete.json.return_value = {
            "message": {"content": '{"title":"T","scenes":[]}'},
            "done_reason": "stop",
        }
        config = {"ollama": {"host": "http://localhost:11434", "model": "qwen3.5:latest", "num_predict": 2048}}
        with patch("core.script_gen.requests.post", side_effect=[truncated, complete]) as post:
            result = _call_ollama(config, "return JSON")
        self.assertEqual(result, {"title": "T", "scenes": []})
        self.assertEqual(post.call_count, 2)
        first, second = [call.kwargs["json"] for call in post.call_args_list]
        self.assertEqual(first["options"]["num_predict"], 2048)
        self.assertEqual(second["options"]["num_predict"], 4096)
        self.assertIs(first["think"], False)
        self.assertIs(second["think"], False)

    def test_parser_accepts_fences_and_short_preamble(self):
        result = _decode_json_content("```json\n{\"title\":\"T\"}\n```")
        self.assertEqual(result, {"title": "T"})

    def test_empty_content_retries_with_thinking_enabled_before_failing(self):
        empty = MagicMock()
        empty.json.return_value = {"message": {"content": "", "thinking": "internal output"}}
        valid = MagicMock()
        valid.json.return_value = {"message": {"content": '{"title":"T"}'}}
        config = {"ollama": {"host": "http://localhost:11434", "model": "qwen3.5:latest"}}
        with patch("core.script_gen.requests.post", side_effect=[empty, valid]) as post:
            result = _call_ollama(config, "return JSON")
        self.assertEqual(result, {"title": "T"})
        self.assertEqual(post.call_count, 2)
        self.assertIs(post.call_args_list[0].kwargs["json"]["think"], False)
        self.assertIs(post.call_args_list[1].kwargs["json"]["think"], True)

    def test_empty_content_error_is_actionable_after_retry(self):
        response = MagicMock()
        response.json.return_value = {"message": {"content": "", "thinking": "internal output"}}
        config = {"ollama": {"host": "http://localhost:11434", "model": "qwen3.5:latest"}}
        with patch("core.script_gen.requests.post", side_effect=[response, response]):
            with self.assertRaisesRegex(RuntimeError, "could not return a valid script JSON.*thinking channel"):
                _call_ollama(config, "return JSON")


if __name__ == "__main__":
    unittest.main()
