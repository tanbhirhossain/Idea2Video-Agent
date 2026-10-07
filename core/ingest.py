"""Turn an idea / blog URL / RSS item into a plain-text brief."""
import re
import feedparser
import trafilatura


def from_idea(text: str) -> str:
    return text.strip()


def from_url(url: str) -> str:
    html = trafilatura.fetch_url(url)
    text = trafilatura.extract(html) if html else None
    if not text:
        raise RuntimeError(f"Could not extract article text from {url}")
    return text[:8000]


def list_feed(feed_url: str):
    feed = feedparser.parse(feed_url)
    items = []
    for e in feed.entries:
        summary = re.sub(r"<[^>]+>", "", e.get("summary", ""))
        items.append({"title": e.get("title", ""), "summary": summary, "link": e.get("link", "")})
    return items


def from_feed_item(item: dict) -> str:
    try:
        return item["title"] + "\n\n" + from_url(item["link"])
    except Exception:
        return item["title"] + "\n\n" + item["summary"]
