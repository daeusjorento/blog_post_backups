#!/usr/bin/env python3
"""Synchronize readable text backups with published posts on daeus.blog."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path


SITE = "daeus.blog"
API_URL = f"https://public-api.wordpress.com/wp/v2/sites/{SITE}/posts"
ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / ".blog-backup-manifest.json"
EXCLUDED_SLUGS = {"my-favorite-memes-of-the-year-since-2020"}
SPECIAL_FILENAMES = {
    "my-college-personal-essay-2008": "20080101_my_personal_essay_2008.txt",
}
BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "div",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "main",
    "p",
    "pre",
    "section",
}


class VisibleTextRenderer(HTMLParser):
    """Render WordPress HTML into deterministic, readable plain text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0
        self.list_stack: list[dict[str, int | str]] = []
        self.in_row = False
        self.row_cell_count = 0

    def boundary(self) -> None:
        self.parts.append("\n\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag in BLOCK_TAGS:
            self.boundary()
        elif tag == "br":
            self.parts.append("\n")
        elif tag in {"ul", "ol"}:
            self.boundary()
            self.list_stack.append({"tag": tag, "index": 0})
        elif tag == "li":
            self.parts.append("\n")
            if self.list_stack and self.list_stack[-1]["tag"] == "ol":
                self.list_stack[-1]["index"] = int(self.list_stack[-1]["index"]) + 1
                self.parts.append(f"{self.list_stack[-1]['index']}. ")
            else:
                self.parts.append("- ")
        elif tag == "tr":
            self.boundary()
            self.in_row = True
            self.row_cell_count = 0
        elif tag in {"td", "th"}:
            if self.in_row and self.row_cell_count:
                self.parts.append("\t")
            self.row_cell_count += 1
        elif tag == "hr":
            self.boundary()
            self.parts.append("-" * 78)
            self.boundary()

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            if self.ignored_depth:
                self.ignored_depth -= 1
            return
        if self.ignored_depth:
            return
        if tag in BLOCK_TAGS or tag == "li":
            self.boundary()
        elif tag in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self.boundary()
        elif tag == "tr":
            self.in_row = False
            self.boundary()

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        raw = html.unescape("".join(self.parts)).replace("\u00a0", " ")
        lines = []
        for line in raw.splitlines():
            cells = [re.sub(r"[ \f\v]+", " ", cell).strip() for cell in line.split("\t")]
            lines.append("\t".join(cells).strip())

        output: list[str] = []
        previous_blank = True
        for line in lines:
            blank = not line
            if blank and previous_blank:
                continue
            output.append(line)
            previous_blank = blank
        while output and not output[-1]:
            output.pop()
        return "\n".join(output) + "\n"


def render_text(rendered_html: str) -> str:
    renderer = VisibleTextRenderer()
    renderer.feed(rendered_html)
    renderer.close()
    return renderer.text()


def fetch_posts() -> list[dict]:
    fields = "id,date,modified,slug,link,title,content"
    posts: list[dict] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"per_page": 100, "page": page, "_fields": fields})
        request = urllib.request.Request(
            f"{API_URL}?{query}",
            headers={"User-Agent": "daeus-blog-backup/1.0"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            batch = json.load(response)
        posts.extend(batch)
        if len(batch) < 100:
            return posts
        page += 1


def words(value: str) -> set[str]:
    ignored = {"a", "an", "and", "at", "for", "in", "into", "my", "of", "on", "the", "to", "with"}
    return {word for word in re.findall(r"[a-z0-9]+", value.lower()) if word not in ignored}


def canonical_filename(post: dict) -> str:
    date = post["date"][:10].replace("-", "")
    slug = re.sub(r"[^a-z0-9]+", "_", post["slug"].lower()).strip("_")
    return f"{date}_{slug}.txt"


def choose_existing_filename(post: dict, manifest: dict, available: set[Path]) -> str:
    previous = manifest.get("posts", {}).get(post["slug"], {}).get("file")
    if previous and (ROOT / previous).exists():
        available.discard(ROOT / previous)
        return previous

    special = SPECIAL_FILENAMES.get(post["slug"])
    if special and (ROOT / special).exists():
        available.discard(ROOT / special)
        return special

    prefix = post["date"][:10].replace("-", "") + "_"
    candidates = [path for path in available if path.name.startswith(prefix)]
    if candidates:
        post_words = words(post["slug"] + " " + post["title"]["rendered"])
        selected = max(candidates, key=lambda path: len(post_words & words(path.stem)))
        available.discard(selected)
        return selected.name
    return canonical_filename(post)


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {"site": f"https://{SITE}/", "excluded_slugs": sorted(EXCLUDED_SLUGS), "posts": {}}
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def synchronize(check: bool) -> int:
    posts = [post for post in fetch_posts() if post["slug"] not in EXCLUDED_SLUGS]
    old_manifest = load_manifest()
    available = set(ROOT.glob("*.txt"))
    changes: list[tuple[str, Path, str]] = []
    manifest_posts: dict[str, dict] = {}

    for post in sorted(posts, key=lambda item: (item["date"], item["slug"])):
        filename = choose_existing_filename(post, old_manifest, available)
        path = ROOT / filename
        content = render_text(post["content"]["rendered"])
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing != content:
            changes.append(("UPDATED" if path.exists() else "CREATED", path, content))
        manifest_posts[post["slug"]] = {
            "file": filename,
            "modified": post["modified"],
            "plain_text_sha256": sha256(content),
            "source": post["link"],
            "title": html.unescape(post["title"]["rendered"]).replace("\u00a0", " "),
        }

    new_manifest = {
        "site": f"https://{SITE}/",
        "excluded_slugs": sorted(EXCLUDED_SLUGS),
        "posts": manifest_posts,
    }
    manifest_content = json.dumps(new_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    old_manifest_content = MANIFEST_PATH.read_text(encoding="utf-8") if MANIFEST_PATH.exists() else None
    if old_manifest_content != manifest_content:
        changes.append(("UPDATED" if MANIFEST_PATH.exists() else "CREATED", MANIFEST_PATH, manifest_content))

    for action, path, content in changes:
        print(f"{action}: {path.relative_to(ROOT)}")
        if not check:
            path.write_text(content, encoding="utf-8")

    if check and changes:
        print(f"{len(changes)} file(s) are out of date.", file=sys.stderr)
        return 1

    if not check:
        for post in posts:
            entry = new_manifest["posts"][post["slug"]]
            actual = (ROOT / entry["file"]).read_text(encoding="utf-8")
            if sha256(actual) != entry["plain_text_sha256"]:
                raise RuntimeError(f"Verification failed for {entry['file']}")
    print(f"Verified {len(posts)} posts; excluded {len(EXCLUDED_SLUGS)} post(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Report drift without changing files")
    args = parser.parse_args()
    return synchronize(args.check)


if __name__ == "__main__":
    raise SystemExit(main())
