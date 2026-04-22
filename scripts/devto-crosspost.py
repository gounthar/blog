#!/usr/bin/env python3
"""Cross-post Hugo AsciiDoc articles to dev.to.

Usage:
    python3 scripts/devto-crosspost.py [--dry-run] [--limit N] [--draft]
    python3 scripts/devto-crosspost.py --publish-drafts [--dry-run]
    python3 scripts/devto-crosspost.py --init-ids [--dry-run]

Options:
    --draft           Post new articles as drafts (default: publish immediately)
    --publish-drafts  Flip all existing unpublished articles to published
    --init-ids        Populate devto-ids.json from existing dev.to articles (one-time setup)
    --dry-run         Show what would happen without making changes
    --limit N         Process at most N missing articles (non-CI mode only)

CI behaviour (push to main):
    When GITHUB_ACTIONS=true and GITHUB_EVENT_NAME=push the script uses
    git diff HEAD^ HEAD to discover changed .adoc files, then publishes new
    ones (POST) or updates existing ones (PUT) based on devto-ids.json.

Reads API key from DEVTO_API_KEY env var or ~/dev.to.key.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import yaml

REQUEST_DELAY = 1.5  # seconds between API writes to stay under rate limit
API_TIMEOUT = 30     # seconds per HTTP request

BLOG_BASE_URL = "https://bruno.verachten.fr"
DEVTO_API = "https://dev.to/api"
POSTS_DIR = Path(os.environ.get("DEVTO_POSTS_DIR") or Path(__file__).parent.parent / "content" / "posts")
DEVTO_IDS_FILE = Path(__file__).parent / "devto-ids.json"
ADOC_BIN = (
    os.environ.get("DEVTO_ADOC_BIN")
    or shutil.which("asciidoctor")
    or "/home/poddingue/.local/share/gem/ruby/3.3.0/bin/asciidoctor"
)
MAX_TAGS = 4


# ---------------------------------------------------------------------------
# State file: stem → dev.to article ID
# ---------------------------------------------------------------------------

def load_ids() -> dict[str, int]:
    if DEVTO_IDS_FILE.exists():
        return json.loads(DEVTO_IDS_FILE.read_text())
    return {}


def save_ids(ids: dict[str, int]) -> None:
    DEVTO_IDS_FILE.write_text(json.dumps(ids, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def get_api_key() -> str:
    env_key = os.environ.get("DEVTO_API_KEY")
    if env_key:
        return env_key.strip()
    key_path = Path.home() / "dev.to.key"
    try:
        return key_path.read_text().strip()
    except FileNotFoundError:
        raise SystemExit(
            "No API key found. Set the DEVTO_API_KEY environment variable "
            f"or create {key_path} with your dev.to API key."
        )


def devto_get(path: str, api_key: str) -> list | dict:
    req = urllib.request.Request(
        f"{DEVTO_API}{path}",
        headers={"api-key": api_key, "User-Agent": "devto-crosspost/1.0"},
    )
    with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
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
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  ERROR {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        raise


def devto_put(path: str, data: dict, api_key: str) -> dict:
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{DEVTO_API}{path}",
        data=body,
        headers={"api-key": api_key, "Content-Type": "application/json", "User-Agent": "devto-crosspost/1.0"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  ERROR {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        raise


def fetch_existing_articles(api_key: str) -> list[dict]:
    published = devto_get("/articles/me?per_page=100", api_key)
    unpublished = devto_get("/articles/me/unpublished?per_page=100", api_key)
    if not isinstance(published, list) or not isinstance(unpublished, list):
        raise RuntimeError(f"Unexpected API response: {published!r} / {unpublished!r}")
    return published + unpublished


def fetch_existing_titles(api_key: str) -> set[str]:
    return {a["title"].strip().lower() for a in fetch_existing_articles(api_key)}


# ---------------------------------------------------------------------------
# Content helpers
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body. Returns (fm_dict, body_str)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("---", 3)
    if end == -1:
        return {}, text
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
    if r1h.returncode != 0:
        raise RuntimeError(f"asciidoctor (html5) failed: {r1h.stderr[:200]}")
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

    text = re.sub(r'(!\[[^\]]*\]\()((?!https?://)[^)]+)(\))', fix, text)

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
    img = fm.get("image", "")
    if not img:
        return None
    if img.startswith("http"):
        return img
    encoded = urllib.parse.quote(img.lstrip("/"), safe="/")
    return f"{BLOG_BASE_URL}/{encoded}"


def make_originally_published_header(fm: dict, canonical_url: str) -> str:
    date_str = str(fm.get("date", "")).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", date_str)
    if m and canonical_url:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        friendly = d.strftime("%B %-d, %Y")
        return f"*Originally published on {friendly} at [{canonical_url}]({canonical_url})*\n\n---\n\n"
    return ""


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


# ---------------------------------------------------------------------------
# Per-post processing
# ---------------------------------------------------------------------------

def render_post(filepath: Path) -> tuple[dict, str] | None:
    """Parse and convert a post. Returns (fm, gfm) or None on skip/error."""
    text = filepath.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    if fm.get("draft") is True:
        return None
    title = str(fm.get("title", "")).strip()
    if not title:
        print(f"  SKIP (no title): {filepath.name}")
        return None
    try:
        gfm = adoc_to_gfm(body)
    except RuntimeError as e:
        print(f"  SKIP (conversion error): {filepath.name} — {e}")
        return None
    return fm, fix_image_urls(gfm)


def process_post(
    filepath: Path,
    api_key: str,
    dry_run: bool,
    draft_mode: bool = False,
    article_id: int | None = None,
) -> int | None:
    """Publish (POST) or update (PUT) a single post. Returns dev.to article ID."""
    result = render_post(filepath)
    if result is None:
        return None
    fm, gfm = result

    title = str(fm.get("title", "")).strip()
    canonical_url = make_canonical_url(fm, filepath)

    if article_id is not None:
        # Update existing article — always keep it published
        payload = build_payload(fm, gfm, canonical_url, published=True)
        if dry_run:
            print(f"  [DRY-RUN] Would update: {title}")
            return article_id
        devto_put(f"/articles/{article_id}", payload, api_key)
        print(f"  UPDATED: {title}")
        time.sleep(REQUEST_DELAY)
        return article_id
    else:
        # New article
        payload = build_payload(fm, gfm, canonical_url, published=not draft_mode)
        if dry_run:
            action = "draft" if draft_mode else "publish"
            print(f"  [DRY-RUN] Would {action}: {title}")
            print(f"            canonical: {canonical_url}")
            print(f"            tags: {payload['article']['tags']}")
            return None
        resp = devto_post("/articles", payload, api_key)
        action = "POSTED draft" if draft_mode else "PUBLISHED"
        new_id = resp.get("id")
        print(f"  {action}: {title}")
        print(f"    → https://dev.to/dashboard (id={new_id})")
        time.sleep(REQUEST_DELAY)
        return new_id


# ---------------------------------------------------------------------------
# CI mode: git-diff based changed-file detection
# ---------------------------------------------------------------------------

def get_changed_adoc_files(posts_dir: Path) -> list[Path]:
    """Return .adoc files under content/posts/ changed in the last commit."""
    repo_root = Path(__file__).parent.parent
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD^", "HEAD", "--", "content/posts/"],
        capture_output=True, text=True, cwd=repo_root,
    )
    if result.returncode != 0:
        print(f"  git diff failed: {result.stderr.strip()}", file=sys.stderr)
        return []
    changed = []
    for name in result.stdout.strip().splitlines():
        if name.endswith(".adoc"):
            p = repo_root / name
            if p.exists():
                changed.append(p)
    return changed


def run_ci(api_key: str, dry_run: bool, draft_mode: bool) -> None:
    """CI push mode: update changed posts, publish new ones, persist ID map."""
    ids = load_ids()
    changed = get_changed_adoc_files(POSTS_DIR)
    if not changed:
        print("No .adoc files changed in this push. Nothing to do.")
        return

    print(f"Processing {len(changed)} changed post(s)...\n")
    ok = 0
    for filepath in changed:
        stem = filepath.stem
        article_id = ids.get(stem)
        action = "update" if article_id else "publish"
        print(f"→ {filepath.name} ({action})")
        try:
            new_id = process_post(filepath, api_key, dry_run, draft_mode, article_id)
            if new_id is not None:
                ids[stem] = new_id
                ok += 1
        except Exception as e:
            print(f"  FAILED: {e}")

    print(f"\nDone. {ok}/{len(changed)} post(s) processed.")
    if not dry_run:
        save_ids(ids)
        print(f"ID map saved to {DEVTO_IDS_FILE.name}")
        print("Review at: https://dev.to/dashboard")
    if ok < len(changed):
        sys.exit(1)


# ---------------------------------------------------------------------------
# --init-ids: one-time population of the state file from existing articles
# ---------------------------------------------------------------------------

def init_ids(api_key: str, posts_dir: Path, dry_run: bool) -> None:
    """Match local posts to existing dev.to articles and write devto-ids.json."""
    print("Fetching all dev.to articles...")
    articles = fetch_existing_articles(api_key)
    title_to_id = {a["title"].strip().lower(): a["id"] for a in articles}
    print(f"  {len(articles)} article(s) found on dev.to")

    ids = load_ids()
    matched = 0
    for p in sorted(posts_dir.glob("*.adoc")):
        if p.stem in ids:
            continue
        text = p.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(text)
        if fm.get("draft") is True:
            continue
        title = str(fm.get("title", "")).strip().lower()
        if title in title_to_id:
            ids[p.stem] = title_to_id[title]
            matched += 1
            print(f"  matched: {p.stem} → id={title_to_id[title]}")

    print(f"\nMatched {matched} new post(s). Total entries: {len(ids)}")
    if dry_run:
        print("[DRY-RUN] Would write devto-ids.json")
        return
    save_ids(ids)
    print(f"Saved to {DEVTO_IDS_FILE}")


# ---------------------------------------------------------------------------
# Legacy helpers (non-CI mode)
# ---------------------------------------------------------------------------

def patch_existing_drafts(api_key: str, posts_dir: Path, dry_run: bool) -> None:
    """Add the 'Originally published' header to drafts that are missing it."""
    articles = fetch_existing_articles(api_key)
    drafts = {a["title"].strip().lower(): a for a in articles if not a.get("published", True)}
    if not drafts:
        return

    title_to_file: dict[str, tuple[Path, dict]] = {}
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    dry_run = "--dry-run" in sys.argv
    draft_mode = "--draft" in sys.argv
    publish_drafts = "--publish-drafts" in sys.argv
    do_init_ids = "--init-ids" in sys.argv
    limit = None
    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    # Auto-detect CI push context
    ci_mode = os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("GITHUB_EVENT_NAME") == "push"

    api_key = get_api_key()

    if do_init_ids:
        init_ids(api_key, POSTS_DIR, dry_run)
        return

    if publish_drafts:
        publish_all_drafts(api_key, dry_run)
        return

    if ci_mode:
        run_ci(api_key, dry_run, draft_mode)
        return

    # --- Local / workflow_dispatch: title-based deduplication ---
    print("Fetching existing dev.to articles...")
    existing = fetch_existing_titles(api_key)
    print(f"  {len(existing)} article(s) already on dev.to")
    patch_existing_drafts(api_key, POSTS_DIR, dry_run)

    ids = load_ids()
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
            new_id = process_post(filepath, api_key, dry_run, draft_mode)
            if new_id is not None:
                ids[filepath.stem] = new_id
                ok += 1
        except Exception as e:
            print(f"  FAILED: {e}")

    print(f"\nDone. {ok}/{len(missing)} {action}.")
    if not dry_run:
        save_ids(ids)
        print("Review at: https://dev.to/dashboard")
    if ok < len(missing):
        sys.exit(1)


if __name__ == "__main__":
    main()
