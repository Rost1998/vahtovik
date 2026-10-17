#!/usr/bin/env python3
"""Print RSS/Atom entries not seen before in --state-dir. Standard library only."""

import argparse
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ATOM = "{http://www.w3.org/2005/Atom}"
MAX_SEEN = 5000


def text(node, *paths):
    for path in paths:
        found = node.find(path)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def clean(value, limit=600):
    value = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    value = " ".join(value.split())
    return value if len(value) <= limit else value[:limit] + "..."


def parse(data):
    root = ET.fromstring(data)
    entries = []
    if root.tag == f"{ATOM}feed":
        title = text(root, f"{ATOM}title")
        for e in root.findall(f"{ATOM}entry"):
            link = next((l.get("href", "") for l in e.findall(f"{ATOM}link") if l.get("rel", "alternate") == "alternate"), "")
            entries.append({
                "id": text(e, f"{ATOM}id") or link,
                "title": clean(text(e, f"{ATOM}title"), 300),
                "link": link,
                "published": text(e, f"{ATOM}published", f"{ATOM}updated"),
                "summary": clean(text(e, f"{ATOM}summary", f"{ATOM}content")),
            })
    else:
        channel = root.find("channel")
        if channel is None:
            raise ValueError("not an RSS or Atom feed")
        title = text(channel, "title")
        for item in channel.findall("item"):
            link = text(item, "link")
            entries.append({
                "id": text(item, "guid") or link,
                "title": clean(text(item, "title"), 300),
                "link": link,
                "published": text(item, "pubDate", "{http://purl.org/dc/elements/1.1/}date"),
                "summary": clean(text(item, "description", "{http://purl.org/rss/1.0/modules/content/}encoded")),
            })
    return title, entries


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--max", type=int, default=50)
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    seen_file = state_dir / "rss-seen.json"
    seen = json.loads(seen_file.read_text()) if seen_file.exists() else []

    try:
        request = urllib.request.Request(args.url, headers={"User-Agent": "vahtovik/0.1 (+rss source skill)"})
        with urllib.request.urlopen(request, timeout=30) as response:
            title, entries = parse(response.read())
    except Exception as exc:  # report any fetch or parse problem to the agent
        print(json.dumps({"feed": args.url, "new": [], "error": f"{type(exc).__name__}: {exc}"}))
        return 0

    known = set(seen)
    unseen = [e for e in entries if e["id"] and e["id"] not in known]
    new = unseen[: args.max]  # older unseen entries beyond max are skipped, not saved for later
    seen = (seen + [e["id"] for e in unseen])[-MAX_SEEN:]
    seen_file.write_text(json.dumps(seen))
    print(json.dumps({"feed": title or args.url, "new": new, "error": None}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
