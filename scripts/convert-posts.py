#!/usr/bin/env python3
"""Convert Jekyll Markdown posts to Hugo AsciiDoc posts.

Reads from Jekyll _posts/ directory, converts Markdown body to AsciiDoc,
preserves YAML front matter, and writes .adoc files to Hugo content/posts/.
"""

import os
import re
import sys
import yaml


JEKYLL_POSTS = os.path.join(os.path.dirname(__file__), "..", "..", "blog", "_posts")
HUGO_POSTS = os.path.join(os.path.dirname(__file__), "..", "content", "posts")

# Also convert drafts
JEKYLL_FUTURE = os.path.join(os.path.dirname(__file__), "..", "..", "blog", ".future_posts")
JEKYLL_FUTURE2 = os.path.join(os.path.dirname(__file__), "..", "..", "blog", "_future_posts")


def split_front_matter(content: str) -> tuple[dict, str]:
    """Split YAML front matter from body."""
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    fm = yaml.safe_load(parts[1]) or {}
    body = parts[2].lstrip("\n")
    return fm, body


def convert_headings(text: str) -> str:
    """Convert Markdown headings to AsciiDoc."""
    def replace_heading(m):
        level = len(m.group(1))
        title = m.group(2).strip()
        # Markdown ## = h2, AsciiDoc == = h2 (same count)
        # But Markdown # (h1) must become == (h2) because = is document title in AsciiDoc
        # Blog posts already have their title in front matter
        if level == 1:
            level = 2
        return "=" * level + " " + title
    return re.sub(r"^(#{1,6})\s+(.+)$", replace_heading, text, flags=re.MULTILINE)


def convert_bold_italic(text: str) -> str:
    """Convert Markdown bold/italic to AsciiDoc."""
    # Bold: **text** -> *text* (but avoid converting *** which is bold+italic)
    # Handle bold+italic first: ***text*** -> *_text_*
    text = re.sub(r"\*{3}([^*\n]+?)\*{3}", r"*_\1_*", text)
    # Bold: **text** -> *text*
    text = re.sub(r"\*{2}([^*\n]+?)\*{2}", r"*\1*", text)
    # Italic: *text* -> _text_ (but not inside code blocks or URLs)
    # Only convert single asterisks that are clearly italic markers
    text = re.sub(r"(?<![*\w])(\*)([^*\n]+?)\1(?![*\w])", r"_\2_", text)
    return text


def convert_links(text: str) -> str:
    """Convert Markdown links to AsciiDoc."""
    # [text](url) -> url[text]
    # But NOT image links (handled separately)
    def replace_link(m):
        link_text = m.group(1)
        url = m.group(2)
        return f"link:{url}[{link_text}]"
    return re.sub(r"(?<!!)\[([^\]]+?)\]\(([^)]+?)\)", replace_link, text)


def convert_images(text: str) -> str:
    """Convert Markdown images to AsciiDoc."""
    # ![alt](path) -> image::path[alt]
    # Also handle {: style="..." width="..."} attributes
    def replace_image(m):
        alt = m.group(1)
        path = m.group(2)
        attrs = m.group(3) or ""
        # Parse Jekyll kramdown attributes
        width = ""
        if attrs:
            w = re.search(r'width="?(\d+[%px]*)"?', attrs)
            if w:
                width = w.group(1)
        attr_parts = [alt]
        if width:
            attr_parts.append(f"width={width}")
        return f"image::{path}[{', '.join(attr_parts)}]"
    return re.sub(
        r"!\[([^\]]*?)\]\(([^)]+?)\)(\{[^}]*\})?",
        replace_image,
        text,
    )


def convert_code_blocks(text: str) -> str:
    """Convert fenced code blocks to AsciiDoc."""
    def replace_code(m):
        lang = m.group(1) or ""
        code = m.group(2)
        if lang:
            return f"[source,{lang}]\n----\n{code}\n----"
        else:
            return f"----\n{code}\n----"
    return re.sub(
        r"```(\w*)\n(.*?)```",
        replace_code,
        text,
        flags=re.DOTALL,
    )


def convert_inline_code(text: str) -> str:
    """Convert inline code `code` to AsciiDoc `code` (same syntax, no change needed)."""
    # AsciiDoc uses backticks for inline code too, so no conversion needed
    return text


def convert_tables(text: str) -> str:
    """Convert Markdown pipe tables to AsciiDoc tables."""
    lines = text.split("\n")
    result = []
    i = 0
    while i < len(lines):
        # Detect table: line with | delimiters
        if re.match(r"^\s*\|.*\|", lines[i]):
            table_lines = []
            while i < len(lines) and re.match(r"^\s*\|.*\|", lines[i]):
                table_lines.append(lines[i])
                i += 1
            result.append(convert_table_block(table_lines))
        else:
            result.append(lines[i])
            i += 1
    return "\n".join(result)


def convert_table_block(table_lines: list[str]) -> str:
    """Convert a block of Markdown table lines to AsciiDoc."""
    if len(table_lines) < 2:
        return "\n".join(table_lines)

    # Parse cells
    rows = []
    separator_idx = None
    for idx, line in enumerate(table_lines):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # Check if this is a separator line (---|---|---)
        if all(re.match(r"^[-:]+$", c.strip()) for c in cells if c.strip()):
            separator_idx = idx
            continue
        rows.append(cells)

    if not rows:
        return "\n".join(table_lines)

    # Determine column count
    num_cols = max(len(r) for r in rows)

    # Build AsciiDoc table
    adoc = [f"[cols=\"{',' .join(['1'] * num_cols)}\", options=\"header\"]"]
    adoc.append("|===")
    for idx, row in enumerate(rows):
        for cell in row:
            adoc.append(f"| {cell}")
        adoc.append("")  # blank line between rows
    adoc.append("|===")
    return "\n".join(adoc)


def convert_youtube_iframes(text: str) -> str:
    """Convert YouTube iframes to AsciiDoc video macro."""
    def replace_youtube(m):
        video_id = m.group(1) or m.group(2)
        return f"video::{video_id}[youtube]"
    # Match various YouTube iframe embed patterns
    text = re.sub(
        r'<iframe[^>]*src="(?:https?:)?//(?:www\.)?youtube\.com/embed/([^"?]+)[^"]*"[^>]*>(?:</iframe>)?',
        replace_youtube,
        text,
    )
    # Also match youtu.be style
    text = re.sub(
        r'<iframe[^>]*src="(?:https?:)?//(?:www\.)?youtu\.be/([^"?]+)[^"]*"[^>]*>(?:</iframe>)?',
        replace_youtube,
        text,
    )
    return text


def convert_html_elements(text: str) -> str:
    """Convert common HTML elements to AsciiDoc equivalents."""
    # <br/> or <br> -> hardbreak
    text = re.sub(r"<br\s*/?>", " +\n", text)
    # <hr> -> '''
    text = re.sub(r"<hr\s*/?>", "'''", text)
    # <strong> -> *
    text = re.sub(r"<strong>(.*?)</strong>", r"*\1*", text)
    # <em> -> _
    text = re.sub(r"<em>(.*?)</em>", r"_\1_", text)
    # <code> -> `
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text)
    return text


def convert_blockquotes(text: str) -> str:
    """Convert Markdown blockquotes to AsciiDoc."""
    lines = text.split("\n")
    result = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("> "):
            quote_lines = []
            while i < len(lines) and (lines[i].startswith("> ") or lines[i].startswith(">")):
                quote_lines.append(lines[i].lstrip("> ").lstrip(">"))
                i += 1
            result.append("[quote]")
            result.append("____")
            result.extend(quote_lines)
            result.append("____")
        else:
            result.append(lines[i])
            i += 1
    return "\n".join(result)


def convert_lists(text: str) -> str:
    """Convert Markdown unordered lists to AsciiDoc."""
    # Markdown uses - or * for unordered lists; AsciiDoc uses *
    # Markdown uses 1. for ordered lists; AsciiDoc uses .
    text = re.sub(r"^(\s*)- ", lambda m: m.group(1) + "* ", text, flags=re.MULTILINE)
    text = re.sub(r"^(\s*)\d+\. ", lambda m: m.group(1) + ". ", text, flags=re.MULTILINE)
    return text


def convert_body(body: str) -> str:
    """Apply all conversions to the Markdown body."""
    # Order matters: code blocks first to protect their content
    body = convert_code_blocks(body)
    body = convert_youtube_iframes(body)
    body = convert_html_elements(body)
    body = convert_tables(body)
    body = convert_blockquotes(body)
    body = convert_images(body)
    body = convert_links(body)
    body = convert_headings(body)
    body = convert_bold_italic(body)
    body = convert_lists(body)
    return body


def build_front_matter(fm: dict, is_draft: bool = False) -> str:
    """Build YAML front matter for Hugo."""
    # Hugo uses the same YAML front matter
    hugo_fm = {}

    # Direct mappings
    for key in ["title", "description", "excerpt", "image", "featured", "hidden",
                 "rating", "toc", "beforetoc", "last_modified_at"]:
        if key in fm:
            hugo_fm[key] = fm[key]

    # Author
    if "author" in fm:
        hugo_fm["author"] = fm["author"]
    else:
        hugo_fm["author"] = "Bruno Verachten"

    # Date (Hugo uses 'date')
    if "date" in fm:
        hugo_fm["date"] = fm["date"]

    # Categories and tags (Hugo uses these directly)
    if "categories" in fm:
        hugo_fm["categories"] = fm["categories"]
    if "tags" in fm:
        hugo_fm["tags"] = fm["tags"]

    # Draft status
    if is_draft:
        hugo_fm["draft"] = True

    return "---\n" + yaml.dump(hugo_fm, default_flow_style=False, allow_unicode=True).strip() + "\n---"


def convert_file(src_path: str, dst_dir: str, is_draft: bool = False) -> str:
    """Convert a single Jekyll post to Hugo AsciiDoc."""
    with open(src_path, "r", encoding="utf-8") as f:
        content = f.read()

    fm, body = split_front_matter(content)
    if not fm:
        print(f"  WARNING: No front matter found in {src_path}")
        return ""

    # Convert body from Markdown to AsciiDoc
    adoc_body = convert_body(body)

    # Build new front matter
    new_fm = build_front_matter(fm, is_draft)

    # Determine output filename
    basename = os.path.basename(src_path)
    # Remove .md extension, add .adoc
    if basename.endswith(".md"):
        basename = basename[:-3] + ".adoc"
    elif basename.endswith(".markdown"):
        basename = basename[:-9] + ".adoc"

    # Skip CLAUDE.md and similar
    if basename.startswith("CLAUDE"):
        return ""

    dst_path = os.path.join(dst_dir, basename)
    os.makedirs(dst_dir, exist_ok=True)

    output = new_fm + "\n\n" + adoc_body
    # Ensure file ends with newline
    if not output.endswith("\n"):
        output += "\n"

    with open(dst_path, "w", encoding="utf-8") as f:
        f.write(output)

    return dst_path


def main():
    jekyll_posts = os.path.abspath(JEKYLL_POSTS)
    hugo_posts = os.path.abspath(HUGO_POSTS)

    if not os.path.isdir(jekyll_posts):
        print(f"ERROR: Jekyll posts directory not found: {jekyll_posts}")
        sys.exit(1)

    os.makedirs(hugo_posts, exist_ok=True)

    # Convert published posts
    md_files = sorted(f for f in os.listdir(jekyll_posts) if f.endswith((".md", ".markdown")) and not f.startswith("CLAUDE"))
    print(f"Converting {len(md_files)} published posts...")

    converted = 0
    for md_file in md_files:
        src = os.path.join(jekyll_posts, md_file)
        dst = convert_file(src, hugo_posts, is_draft=False)
        if dst:
            converted += 1
            print(f"  OK: {md_file} -> {os.path.basename(dst)}")

    # Convert drafts from .future_posts
    for draft_dir in [JEKYLL_FUTURE, JEKYLL_FUTURE2]:
        draft_dir = os.path.abspath(draft_dir)
        if not os.path.isdir(draft_dir):
            continue
        draft_files = sorted(f for f in os.listdir(draft_dir) if f.endswith((".md", ".markdown")))
        print(f"\nConverting {len(draft_files)} drafts from {os.path.basename(draft_dir)}...")
        for md_file in draft_files:
            src = os.path.join(draft_dir, md_file)
            dst = convert_file(src, hugo_posts, is_draft=True)
            if dst:
                converted += 1
                print(f"  OK (draft): {md_file} -> {os.path.basename(dst)}")

    print(f"\nDone! Converted {converted} files total.")
    print(f"Output directory: {hugo_posts}")


if __name__ == "__main__":
    main()
