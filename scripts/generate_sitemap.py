#!/usr/bin/env python3
"""Generate sitemap.xml for titaniablesh.com from all HTML files in the repo."""

import subprocess
from datetime import datetime
from pathlib import Path

BASE_URL = "https://titaniablesh.com"
SITE_ROOT = Path(__file__).parent.parent


def get_last_modified(file_path: Path) -> str:
    """Get the last git commit date for a file, falling back to today."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ci", str(file_path)],
            capture_output=True, text=True, cwd=SITE_ROOT
        )
        date_str = result.stdout.strip()
        if date_str:
            return date_str[:10]  # YYYY-MM-DD
    except Exception:
        pass
    return datetime.today().strftime("%Y-%m-%d")


def get_url_and_priority(file_path: Path):
    """Convert file path to URL and assign priority. Returns None to exclude."""
    rel = file_path.relative_to(SITE_ROOT)
    parts = rel.parts

    # Exclude non-page directories
    if parts[0] in ("assets", "scripts", ".github"):
        return None

    # Convert path to URL
    if rel.name == "index.html":
        url_path = "/" + "/".join(parts[:-1])
        if url_path != "/":
            url_path += "/"
    else:
        url_path = "/" + "/".join(parts)

    url = BASE_URL + url_path

    # Assign priority by path
    if url_path == "/":
        priority = 1.0
    elif url_path in ("/it/", "/rights/"):
        priority = 0.9
    elif url_path == "/blog/":
        priority = 0.8
    elif "drafts/english" in url_path:
        priority = 0.8
    elif url_path == "/volcanoes/":
        priority = 0.5
    else:
        priority = 0.6

    changefreq = "monthly" if priority >= 0.9 else "yearly"
    return url, priority, changefreq


def generate_sitemap():
    entries = []

    for html_file in sorted(SITE_ROOT.rglob("*.html")):
        result = get_url_and_priority(html_file)
        if result is None:
            continue
        url, priority, changefreq = result
        lastmod = get_last_modified(html_file)
        entries.append((priority, url, lastmod, changefreq))

    # Sort by priority descending, then alphabetically
    entries.sort(key=lambda x: (-x[0], x[1]))

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        "",
    ]

    for priority, url, lastmod, changefreq in entries:
        lines += [
            "  <url>",
            f"    <loc>{url}</loc>",
            f"    <lastmod>{lastmod}</lastmod>",
            f"    <changefreq>{changefreq}</changefreq>",
            f"    <priority>{priority:.1f}</priority>",
            "  </url>",
        ]

    lines += ["", "</urlset>", ""]

    sitemap_path = SITE_ROOT / "sitemap.xml"
    sitemap_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Generated sitemap.xml with {len(entries)} URLs")


if __name__ == "__main__":
    generate_sitemap()
