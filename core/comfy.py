"""Minimal ComfyUI HTTP client: inject params into an API-format workflow, queue, poll, download."""
import json
import time
import uuid
from pathlib import Path
import requests


class ComfyClient:
    def __init__(self, host: str, timeout_s: int = 1800):
        self.host = host.rstrip("/")
        self.timeout = timeout_s
        self.client_id = str(uuid.uuid4())

    @staticmethod
    def _load(spec):
        return json.loads(Path(spec["workflow"]).read_text(encoding="utf-8"))

    def validate(self, spec: dict):
        """Fail early with a clear message if the server is down or the node map doesn't match the workflow."""
        try:
            requests.get(f"{self.host}/system_stats", timeout=5).raise_for_status()
        except Exception as e:
            raise RuntimeError(f"ComfyUI not reachable at {self.host} ({e}). Is it running / is the port right?")
        wf = self._load(spec)
        for key, (node, field) in spec["map"].items():
            if str(node) not in wf:
                raise RuntimeError(f"config map '{key}': node {node} not found in {spec['workflow']}")
            if field not in wf[str(node)]["inputs"]:
                raise RuntimeError(f"config map '{key}': node {node} has no input '{field}'")

    def combo_options(self, spec, key):
        node, field = spec["map"][key]
        cls = self._load(spec)[str(node)]["class_type"]
        info = requests.get(f"{self.host}/object_info/{cls}", timeout=10).json()[cls]["input"]
        entry = {**info.get("required", {}), **info.get("optional", {})}[field]
        return entry[0] if isinstance(entry[0], list) else entry[1].get("options", [])

    def resolve_aspect(self, spec, aspect: str):
        """Map '9:16' to the exact option string the ResolutionSelector node accepts."""
        if "aspect" not in spec["map"]:
            return None
        try:
            for o in self.combo_options(spec, "aspect"):
                if str(o).startswith(aspect):
                    return o
        except Exception:
            pass
        return spec.get("aspect_fallback", {}).get(aspect, aspect)

    def generate(self, spec: dict, values: dict, out_stem: Path) -> Path:
        for attempt in range(3):
            try:
                return self._generate_once(spec, values, out_stem)
            except RuntimeError as e:
                if "memory" not in str(e).lower() or attempt == 2:
                    raise
                # GPU OOM: drop quality and retry (lighter latent + lightning/turbo lora if available)
                if "megapixels" in spec["map"]:
                    values["megapixels"] = 1
                if "turbo" in spec["map"]:
                    values["turbo"] = True
                if "duration" in spec["map"]:
                    values["duration"] = min(float(values.get("duration", 6)), 5.0)
                time.sleep(15)
        raise RuntimeError("generation failed")

    def _generate_once(self, spec: dict, values: dict, out_stem: Path) -> Path:
        wf = self._load(spec)
        values = {**spec.get("fixed", {}), **{k: v for k, v in values.items() if v is not None}}
        for key, val in values.items():
            if key in spec["map"]:
                node, field = spec["map"][key]
                wf[str(node)]["inputs"][field] = val

        r = requests.post(f"{self.host}/prompt", json={"prompt": wf, "client_id": self.client_id})
        if r.status_code != 200:
            raise RuntimeError(f"ComfyUI rejected workflow: {r.text}")
        pid = r.json()["prompt_id"]

        t0 = time.time()
        while time.time() - t0 < self.timeout:
            h = requests.get(f"{self.host}/history/{pid}").json()
            if pid in h:
                entry = h[pid]
                if entry.get("status", {}).get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI job failed: {entry['status']}")
                f = self._pick_output(entry.get("outputs", {}))
                if f:
                    return self._download(f, out_stem)
            time.sleep(2)
        raise TimeoutError("ComfyUI generation timed out")

    @staticmethod
    def _pick_output(outputs: dict):
        found = []
        for node_out in outputs.values():
            for items in node_out.values():
                if isinstance(items, list):
                    found += [i for i in items if isinstance(i, dict) and "filename" in i]
        found.sort(key=lambda i: i.get("type") != "output")  # prefer saved outputs over temp previews
        return found[0] if found else None

    def _download(self, f: dict, out_stem: Path) -> Path:
        r = requests.get(f"{self.host}/view", params={
            "filename": f["filename"], "subfolder": f.get("subfolder", ""), "type": f.get("type", "output")})
        r.raise_for_status()
        out = out_stem.with_suffix(Path(f["filename"]).suffix)
        out.write_bytes(r.content)
        return out
