#!/usr/bin/env python3
"""Fetch a web page, compare its text and links with the last snapshot in --state-dir."""

import argparse
import difflib
import gzip
import json
import sys
import urllib.request
import zlib
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urldefrag

BLOCK = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
         "footer", "td", "th", "dt", "dd", "pre", "blockquote"}
SKIP = {"script", "style", "noscript", "svg", "template"}
FIRST_RUN_LINES = 200


class PageParser(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.parts, self.links = base, [], []
        self.skip_depth, self.href, self.link_text = 0, None, []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip_depth += 1
        elif tag in BLOCK:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("javascript:", "mailto:", "#")):
                self.href, self.link_text = urldefrag(urljoin(self.base, href))[0], []

    def handle_endtag(self, tag):
        if tag in SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif tag in BLOCK:
            self.parts.append("\n")
        if tag == "a" and self.href:
            self.links.append({"text": " ".join("".join(self.link_text).split())[:200], "href": self.href})
            self.href = None

    def handle_data(self, data):
        if self.skip_depth:
            return
        self.parts.append(data)
        if self.href:
            self.link_text.append(data)

    def lines(self):
        text = "".join(self.parts)
        return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    snapshot_file, links_file = state_dir / "page.txt", state_dir / "page-links.json"

    try:
        request = urllib.request.Request(args.url, headers={
            "User-Agent": "Mozilla/5.0 (vahtovik web-page skill)", "Accept-Encoding": "gzip, deflate"})
        with urllib.request.urlopen(request, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read()
            encoding = (response.headers.get("Content-Encoding") or "").lower()
            if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            elif encoding == "deflate":
                raw = zlib.decompress(raw)
            page = raw.decode(charset, errors="replace")
    except Exception as exc:  # report any fetch problem to the agent
        print(json.dumps({"url": args.url, "error": f"{type(exc).__name__}: {exc}"}))
        return 0

    html = PageParser(args.url)
    html.feed(page)
    lines = html.lines()
    links = list({l["href"]: l for l in html.links}.values())

    first_run = not snapshot_file.exists()
    if first_run:
        added = lines[:FIRST_RUN_LINES]
        new_links = links
    else:
        old = snapshot_file.read_text().splitlines()
        added = [l[1:] for l in difflib.unified_diff(old, lines, lineterm="", n=0)
                 if l.startswith("+") and not l.startswith("+++")]
        old_links = {l["href"] for l in json.loads(links_file.read_text())} if links_file.exists() else set()
        new_links = [l for l in links if l["href"] not in old_links]

    snapshot_file.write_text("\n".join(lines))
    links_file.write_text(json.dumps(links))
    print(json.dumps({"url": args.url, "first_run": first_run, "added_lines": added,
                      "new_links": new_links, "error": None}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
