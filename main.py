import argparse
import time
from pathlib import Path
import yaml

from core import ingest
from core.pipeline import build


def main():
    ap = argparse.ArgumentParser(description="Idea -> Video automation")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--idea", help="Free-text idea")
    src.add_argument("--url", help="Blog / news article URL")
    src.add_argument("--feed", help="RSS/Atom feed URL (lists items to pick from)")
    ap.add_argument("--pick", type=int, help="Feed item index (skips the prompt)")
    ap.add_argument("--mode", choices=["image", "video"], default="image")
    ap.add_argument("--format", choices=["shorts", "video", "custom"], default="shorts")
    ap.add_argument("--size", help="Custom size WxH, e.g. 1080x1350")
    ap.add_argument("--seconds", type=int, default=45)
    ap.add_argument("--language", default="English")
    ap.add_argument("--job", help="Job folder (reuse to resume)")
    ap.add_argument("--config", default="config.yaml")
    a = ap.parse_args()

    cfg = yaml.safe_load(open(a.config, encoding="utf-8"))
    if a.idea:
        text = ingest.from_idea(a.idea)
    elif a.url:
        text = ingest.from_url(a.url)
    else:
        items = ingest.list_feed(a.feed)
        for i, it in enumerate(items[:20]):
            print(f"[{i}] {it['title']}")
        pick = a.pick if a.pick is not None else int(input("Pick item #: "))
        text = ingest.from_feed_item(items[pick])

    custom = tuple(int(x) for x in a.size.lower().split("x")) if a.size else None
    if a.format == "custom" and not custom:
        ap.error("--format custom needs --size WxH")

    job = Path(a.job or f"output/job_{int(time.time())}")
    final = build(cfg, job, text, a.mode, a.format, custom, a.seconds, a.language)
    print("DONE ->", final)


if __name__ == "__main__":
    main()
