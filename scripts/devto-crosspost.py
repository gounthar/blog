#!/usr/bin/env python3
"""Cross-post Hugo AsciiDoc articles to dev.to.

Usage:
    python3 scripts/devto-crosspost.py [--dry-run] [--limit N] [--draft]
    python3 scripts/devto-crosspost.py --publish-drafts [--dry-run]

Options:
    --draft           Post new articles as drafts (default: publish immediately)
    --publish-drafts  Flip all existing unpublished articles to published
    --dry-run         Show what would happen without making changes
    --limit N         Process at most N missing articles

Reads API key from ~/dev.to.key.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

REQUEST_DELAY = 1.5  # seconds between API writes to stay under rate limit

BLOG_BASE_URL = "https://bruno.verachten.fr"
DEVTO_API = "https://dev.to/api"
POSTS_DIR = Path("/mnt/c/support/users/blog-hugo/content/posts")
ADOC_BIN = (
    os.environ.get("DEVTO_ADOC_BIN")
    or shutil.which("asciidoctor")
    or "/home/poddingue/.local/share/gem/ruby/3.3.0/bin/asciidoctor"
)
MAX_TAGS = 4


def get_api_key() -> str:
    env_key = os.environ.get("DEVTO_API_KEY")
    if env_key:
        return env_key.strip()
    return (Path.home() / "dev.to.key").read_text().strip()


def devto_get(path: str, api_key: str) -> list | dict:
    req = urllib.request.Request(
        f"{DEVTO_API}{path}",
        headers={"api-key": api_key, "User-Agent": "devto-crosspost/1.0"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def devto_post(path: str, data: dict, api_key: str) -> dict:
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{DEVTO_API}{path}",
        data=body,
        headers={"api-key": api_key, "Content-Type": "application/json", "User-Agent": "devto-crosspost/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  ERROR {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        raise


def fetch_existing_articles(api_key: str) -> list[dict]:
    published = devto_get("/articles/me?per_page=100", api_key)
    unpublished = devto_get("/articles/me/unpublished?per_page=100", api_key)
    return published + unpublished


def fetch_existing_titles(api_key: str) -> set[str]:
    return {a["title"].strip().lower() for a in fetch_existing_articles(api_key)}


def devto_put(path: str, data: dict, api_key: str) -> dict:
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{DEVTO_API}{path}",
        data=body,
        headers={"api-key": api_key, "Content-Type": "application/json", "User-Agent": "devto-crosspost/1.0"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  ERROR {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        raise


def patch_existing_drafts(api_key: str, posts_dir: Path, dry_run: bool) -> None:
    """Add the 'Originally published' header to drafts that are missing it."""
    articles = fetch_existing_articles(api_key)
    drafts = {a["title"].strip().lower(): a for a in articles if not a.get("published", True)}
    if not drafts:
        return

    title_to_file = {}
    for p in posts_dir.glob("*.adoc"):
        text = p.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(text)
        if fm.get("draft") is True:
            continue
        title = str(fm.get("title", "")).strip().lower()
        title_to_file[title] = (p, fm)

    print("\nChecking existing drafts for missing header...")
    for title, article in drafts.items():
        body = article.get("body_markdown", "")
        if "Originally published on" in body:
            continue
        if title not in title_to_file:
            continue
        filepath, fm = title_to_file[title]
        canonical_url = make_canonical_url(fm, filepath)
        header = make_originally_published_header(fm, canonical_url)
        if not header:
            continue
        new_body = header + body
        if dry_run:
            print(f"  [DRY-RUN] Would patch: {article['title']}")
            continue
        devto_put(f"/articles/{article['id']}", {"article": {"body_markdown": new_body}}, api_key)
        print(f"  PATCHED: {article['title']}")
        time.sleep(REQUEST_DELAY)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body. Returns (fm_dict, body_str)."""
    if not text.startswith("---"):
        return {}, text
    end = text.index("---", 3)
    fm = yaml.safe_load(text[3:end]) or {}
    body = text[end + 3:].strip()
    return fm, body


def adoc_to_gfm(body: str) -> str:
    """Convert AsciiDoc to GFM via asciidoctor + pandoc.

    Tries DocBook first (cleaner output). Falls back to HTML5 when DocBook
    produces invalid XML (e.g. URLs auto-linked inside code spans).
    """
    r1 = subprocess.run(
        [ADOC_BIN, "-b", "docbook", "--safe", "-o", "-", "-"],
        input=body, capture_output=True, text=True,
    )
    if r1.returncode != 0:
        raise RuntimeError(f"asciidoctor failed: {r1.stderr[:200]}")

    r2 = subprocess.run(
        ["pandoc", "-f", "docbook", "-t", "gfm", "--wrap=none"],
        input=r1.stdout, capture_output=True, text=True,
    )
    if r2.returncode == 0:
        return r2.stdout

    # DocBook produced invalid XML — fall back to HTML5
    r1h = subprocess.run(
        [ADOC_BIN, "-b", "html5", "--safe", "-o", "-", "-"],
        input=body, capture_output=True, text=True,
    )
    r2h = subprocess.run(
        ["pandoc", "-f", "html", "-t", "gfm", "--wrap=none"],
        input=r1h.stdout, capture_output=True, text=True,
    )
    if r2h.returncode != 0:
        raise RuntimeError(f"pandoc failed (both DocBook and HTML5): {r2h.stderr[:200]}")
    return r2h.stdout


def fix_image_urls(text: str) -> str:
    """Rewrite relative image URLs to absolute bruno.verachten.fr URLs."""
    def fix(m):
        url = m.group(2)
        if url.startswith("http"):
            return m.group(0)
        url = url.lstrip("/")
        return f"{m.group(1)}{BLOG_BASE_URL}/{url}{m.group(3)}"

    # Markdown: ![alt](url)
    text = re.sub(
        r'(!\[[^\]]*\]\()((?!https?://)[^)]+)(\))',
        fix, text,
    )
    # HTML <img src="...">
    def fix_html(m):
        url = m.group(1)
        if url.startswith("http"):
            return m.group(0)
        return f'src="{BLOG_BASE_URL}/{url.lstrip("/")}"'
    text = re.sub(r'src="((?!https?://)[^"]+)"', fix_html, text)
    return text


def make_canonical_url(fm: dict, filepath: Path) -> str:
    """Derive canonical URL from frontmatter date + filename slug."""
    date_str = str(fm.get("date", "")).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", date_str)
    if not m:
        return ""
    year, month, day = m.groups()
    # Strip leading YYYY-MM-DD- from filename to get Hugo slug
    stem = filepath.stem
    slug = re.sub(r"^\d{4}-\d{2}-\d{2}-?", "", stem)
    slug = slug.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return f"{BLOG_BASE_URL}/{year}/{month}/{day}/{slug}/"


def normalize_tags(raw: list | str | None) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [t.strip() for t in raw.split(",")]
    clean = []
    for t in raw:
        t = re.sub(r"[^a-z0-9]", "", t.lower())
        if t:
            clean.append(t)
    return clean[:MAX_TAGS]


def make_cover_image(fm: dict) -> str | None:
    import urllib.parse
    img = fm.get("image", "")
    if not img:
        return None
    if img.startswith("http"):
        return img
    # URL-encode spaces and other unsafe chars in the path
    encoded = urllib.parse.quote(img.lstrip("/"), safe="/")
    return f"{BLOG_BASE_URL}/{encoded}"


def make_originally_published_header(fm: dict, canonical_url: str) -> str:
    date_str = str(fm.get("date", "")).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", date_str)
    if m and canonical_url:
        from datetime import date
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        friendly = d.strftime("%B %-d, %Y")
        return f"*Originally published on {friendly} at [{canonical_url}]({canonical_url})*\n\n---\n\n"
    return ""


def publish_all_drafts(api_key: str, dry_run: bool) -> None:
    """Flip every unpublished article to published."""
    drafts = [a for a in fetch_existing_articles(api_key) if not a.get("published", True)]
    if not drafts:
        print("No unpublished drafts found.")
        return
    print(f"Publishing {len(drafts)} draft(s)...")
    for a in drafts:
        if dry_run:
            print(f"  [DRY-RUN] Would publish: {a['title']}")
            continue
        devto_put(f"/articles/{a['id']}", {"article": {"published": True}}, api_key)
        print(f"  PUBLISHED: {a['title']}")
        time.sleep(REQUEST_DELAY)


def build_payload(fm: dict, markdown: str, canonical_url: str, published: bool = True) -> dict:
    header = make_originally_published_header(fm, canonical_url)
    article = {
        "title": str(fm.get("title", "")).strip(),
        "body_markdown": header + markdown,
        "published": published,
        "tags": normalize_tags(fm.get("tags")),
        "canonical_url": canonical_url,
    }
    if fm.get("description"):
        article["description"] = str(fm["description"])[:150]
    cover = make_cover_image(fm)
    if cover:
        article["main_image"] = cover
    return {"article": article}


def process_post(filepath: Path, api_key: str, dry_run: bool, draft_mode: bool = False) -> bool:
    text = filepath.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)

    if fm.get("draft") is True:
        return False

    title = str(fm.get("title", "")).strip()
    if not title:
        print(f"  SKIP (no title): {filepath.name}")
        return False

    canonical_url = make_canonical_url(fm, filepath)
    try:
        gfm = adoc_to_gfm(body)
    except RuntimeError as e:
        print(f"  SKIP (conversion error): {filepath.name} — {e}")
        return False

    gfm = fix_image_urls(gfm)
    payload = build_payload(fm, gfm, canonical_url, published=not draft_mode)

    if dry_run:
        action = "draft" if draft_mode else "publish"
        print(f"  [DRY-RUN] Would {action}: {title}")
        print(f"            canonical: {canonical_url}")
        print(f"            tags: {payload['article']['tags']}")
        return True

    result = devto_post("/articles", payload, api_key)
    action = "POSTED draft" if draft_mode else "PUBLISHED"
    print(f"  {action}: {title}")
    print(f"    → https://dev.to/dashboard (id={result.get('id')})")
    time.sleep(REQUEST_DELAY)
    return True


def main():
    dry_run = "--dry-run" in sys.argv
    draft_mode = "--draft" in sys.argv
    publish_drafts = "--publish-drafts" in sys.argv
    limit = None
    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    api_key = get_api_key()

    if publish_drafts:
        publish_all_drafts(api_key, dry_run)
        return

    print("Fetching existing dev.to articles...")
    existing = fetch_existing_titles(api_key)
    print(f"  {len(existing)} article(s) already on dev.to")
    patch_existing_drafts(api_key, POSTS_DIR, dry_run)

    posts = sorted(POSTS_DIR.glob("*.adoc"))
    missing = []
    for p in posts:
        text = p.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(text)
        if fm.get("draft") is True:
            continue
        title = str(fm.get("title", "")).strip()
        if title.lower() not in existing:
            missing.append(p)

    print(f"  {len(missing)} post(s) missing from dev.to")
    if limit:
        missing = missing[:limit]
        print(f"  (limited to {limit})")

    if not missing:
        print("Nothing to do.")
        return

    action = "drafts" if draft_mode else "published articles"
    mode = "[DRY-RUN] " if dry_run else ""
    print(f"\n{mode}Processing {len(missing)} post(s) as {action}...\n")
    ok = 0
    for filepath in missing:
        print(f"→ {filepath.name}")
        try:
            if process_post(filepath, api_key, dry_run, draft_mode):
                ok += 1
        except Exception as e:
            print(f"  FAILED: {e}")

    print(f"\nDone. {ok}/{len(missing)} {action}.")
    if not dry_run:
        print("Review at: https://dev.to/dashboard")


if __name__ == "__main__":
    main()
