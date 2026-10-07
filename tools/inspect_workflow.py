"""List nodes of an API-format ComfyUI workflow so you can fill config.yaml's `map`."""
import json
import sys

wf = json.load(open(sys.argv[1], encoding="utf-8"))
for nid, node in wf.items():
    print(f"[{nid}] {node.get('class_type')}")
    for k, v in node.get("inputs", {}).items():
        if not isinstance(v, list):  # skip links to other nodes
            s = str(v).replace("\n", " ")
            print(f"      {k} = {s[:70]}")
