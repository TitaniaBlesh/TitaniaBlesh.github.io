#!/usr/bin/env python3
"""
Build static blog from WordPress XML export.

Generates:
  blog/index.html              — category-grouped + recent feed
  blog/<slug>.html              — one per post (32)
  blog/_image_manifest.json     — list of expected images (relative paths) so the
                                   user can verify their uploads folder later
"""
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import unquote, urlparse

# ───────────────────────────────── paths ─────────────────────────────────
ROOT = "/sessions/kind-festive-lamport/mnt/titaniablesh_site"
XML_PATH = "/sessions/kind-festive-lamport/mnt/uploads/titaniablesh.WordPress.2026-05-05.xml"
BLOG_DIR = os.path.join(ROOT, "blog")
IMAGES_DIR = os.path.join(ROOT, "assets/images/blog")
MANIFEST_PATH = os.path.join(BLOG_DIR, "_image_manifest.json")
# Optional WordPress media-library URL list (one URL per line, header skipped).
# When present, we rewrite image references to filenames that actually exist
# in the CSV — peeling WP's auto-generated -WIDTHxHEIGHT / -scaled suffixes off
# variants whose original is in the CSV.
MEDIA_CSV_PATH = "/sessions/kind-festive-lamport/mnt/uploads/export-media-urls-894064.csv"

NS = {
    "wp": "http://wordpress.org/export/1.2/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "excerpt": "http://wordpress.org/export/1.2/excerpt/",
}

# Original blog page used these categories in this order. Map XML categories → display title.
CATEGORY_ORDER = [
    ("Narrativa e Generi", "Generale"),
    ("Struttura Narrativa", "Struttura narrativa e trama"),
    ("Worldbuilding", "Worldbuilding"),
    ("Revisione", "Revisione"),
    ("Pubblicazione", "Pubblicazione"),
    ("Miscellanea", "Miscellanea"),
]


# ───────────────────────────── XML helpers ───────────────────────────────
def parse_xml():
    tree = ET.parse(XML_PATH)
    root = tree.getroot()
    channel = root.find("channel")
    posts = []
    for it in channel.findall("item"):
        pt = it.findtext("wp:post_type", "", NS)
        st = it.findtext("wp:status", "", NS)
        if pt != "post" or st != "publish":
            continue
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        date_raw = it.findtext("wp:post_date", "", NS).strip()
        modified = it.findtext("wp:post_modified", "", NS).strip()
        slug = it.findtext("wp:post_name", "", NS).strip() or _slugify(title)
        content_el = it.find("content:encoded", NS)
        excerpt_el = it.find("excerpt:encoded", NS)
        content = (content_el.text if content_el is not None else "") or ""
        excerpt = (excerpt_el.text if excerpt_el is not None else "") or ""
        cats = [c.text for c in it.findall("category") if c.get("domain") == "category"]
        posts.append(
            {
                "title": title,
                "link": link,
                "slug": slug,
                "date": date_raw,
                "modified": modified,
                "content": content,
                "excerpt": excerpt,
                "categories": cats,
            }
        )
    posts.sort(key=lambda p: p["date"])
    return posts


def _slugify(s):
    s = re.sub(r"[^\w\s-]", "", s.lower())
    return re.sub(r"[\s_-]+", "-", s).strip("-")


# ───────────────────────────── image rewriting ────────────────────────────
# Strategy:
#   Every image URL on the live site looks like
#     https://titaniablesh.com/wp-content/uploads/YYYY/MM/<file>(?-WxH).<ext>
#   or proxied through Jetpack:
#     https://i{0,1,2}.wp.com/titaniablesh.com/wp-content/uploads/YYYY/MM/<file>?w=...&ssl=1
#
#   We collapse both forms to the canonical local path:
#     ../assets/images/blog/YYYY/MM/<file>
#
#   We always keep the original filename (including any -WxH suffix WordPress
#   pre-generated). Dropping the resize query parameters is fine — the browser
#   will load the same physical file at its real size.

UPLOADS_PATH_RE = re.compile(r"/wp-content/uploads/(\d{4}/\d{2}/[^?#\s\"']+)")


# ─── content renames ─────────────────────────────────────────────────
# Phrase-level substitutions applied to every post body during the build.
# Use word-boundary regex pairs (case-insensitive on the pattern, fixed casing
# on the replacement). Add entries here if you ever rename a project/character.
RENAMES = [
    (re.compile(r"\bGas\s+Geyser\b", re.IGNORECASE), "Volcanomancer"),
]


def _apply_renames(text):
    for pat, repl in RENAMES:
        text = pat.sub(repl, text)
    return text


def _load_csv_paths():
    """Load every relative-to-uploads path from the WP media CSV, if present."""
    if not os.path.exists(MEDIA_CSV_PATH):
        return None
    paths = set()
    import csv
    with open(MEDIA_CSV_PATH, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)  # header
        for row in reader:
            if not row:
                continue
            url = row[0].strip()
            m = re.search(r"/wp-content/uploads/(.+)$", url)
            if m:
                paths.add(m.group(1))
    return paths


# Loaded once at import time
CSV_PATHS = _load_csv_paths()


def _resolve_against_csv(rel_path):
    """If `rel_path` isn't in the CSV but the original (sans WP variant
    suffixes) IS, return the original. Otherwise return rel_path unchanged.
    Returns (path, found_in_csv) — the second element is False when neither
    the path nor any candidate original is in the CSV. Callers handling <img>
    tags use this signal to drop unresolvable images entirely."""
    if not CSV_PATHS:
        return rel_path, True  # no CSV loaded → trust the path as-is
    if rel_path in CSV_PATHS:
        return rel_path, True
    # Try peeling off WordPress's auto-generated suffixes.
    folder, fname = rel_path.rsplit("/", 1)
    name, ext = os.path.splitext(fname)
    candidates = []
    n = re.sub(r"-\d+x\d+$", "", name)
    if n != name:
        candidates.append(n)
    n2 = re.sub(r"-scaled$", "", n)
    if n2 != n:
        candidates.append(n2)
    n3 = re.sub(r"-\d+$", "", n2)
    if n3 != n2:
        candidates.append(n3)
    for c in candidates:
        cand = f"{folder}/{c}{ext}"
        if cand in CSV_PATHS:
            return cand, True
    return rel_path, False  # truly missing

# Some posts embed bullet decorations from Google's CDN (Google Drive / Photos),
# inserted at 20×15 — the same size as the local volcano bullet WP uses
# everywhere else. These URLs aren't recoverable once we kill WordPress, so we
# substitute the local volcano bullet wherever they appear.
LOCAL_VOLCANO_BULLET = "2020/07/volcano_bullet-e1594035605298.png"
EXTERNAL_BULLET_RE = re.compile(
    r'<img\s[^>]*src="https?://lh\d\.googleusercontent\.com/[^"]+"[^>]*>',
    re.I,
)
# WordPress emoji fallback served from s.w.org. We strip the <img> entirely —
# modern browsers render the inline unicode just fine.
WP_EMOJI_RE = re.compile(
    r'<img\s[^>]*src="https?://s\.w\.org/images/core/emoji/[^"]+"[^>]*>',
    re.I,
)


def _normalize_external_images(text, image_manifest):
    """Pre-pass before CleanParser: rewrite stray external <img> tags."""
    def _bullet(_m):
        image_manifest.add(LOCAL_VOLCANO_BULLET)
        return (
            f'<img src="../assets/images/blog/{LOCAL_VOLCANO_BULLET}" '
            f'alt="" width="20" height="15" loading="lazy">'
        )
    text = EXTERNAL_BULLET_RE.sub(_bullet, text)
    text = WP_EMOJI_RE.sub("", text)
    return text


def rewrite_url_to_local(url, drop_if_missing=False):
    """Map a WP/jetpack URL to a local relative path.

    Returns (local_url, manifest_path).
    When the CSV is loaded and neither the path nor any peeled-down candidate
    appears in it, and `drop_if_missing` is True, return (None, None) so the
    caller can skip emitting the tag entirely (used for <img> — broken images
    are noisy in the page).
    """
    if not url:
        return url, None
    # decode HTML entities
    url = html.unescape(url)
    m = UPLOADS_PATH_RE.search(url)
    if not m:
        return url, None
    local_relative = m.group(1)  # e.g. 2020/03/Cattura14.png
    # strip any leading ?... that might've slipped through
    local_relative = local_relative.split("?")[0].split("#")[0]
    # If the WP media library only has the original (not the -WxH variant),
    # rewrite to the original — same image, just unscaled.
    local_relative, found = _resolve_against_csv(local_relative)
    if drop_if_missing and not found:
        return None, None
    # path used inside blog/<slug>.html (one level deep): ../assets/images/blog/...
    return f"../assets/images/blog/{local_relative}", local_relative


# ───────────────────────────── content cleaning ──────────────────────────
# WordPress dumps a fair bit of cruft we want to remove from each post body:
#   - srcset / sizes / data-* attrs on <img>
#   - Jetpack lazy-load placeholder, recalc-dims attrs
#   - inline 'fix-anu-...' classes
#   - <p> wrappers around images that produce stray empty paragraphs
#   - WP shortcode-ish leftovers
#
# Rather than write a fragile regex-based cleaner, we use HTMLParser to walk
# the markup and emit a minimal cleaned version.


class CleanParser(HTMLParser):
    """Cleans WordPress post HTML and rewrites image URLs."""

    SELF_CLOSING = {"img", "br", "hr", "meta", "link", "input"}
    DROP_TAGS = {"script", "style", "noscript", "form", "input", "button"}
    # Strip these attributes from any tag.
    DROP_ATTRS = {
        "srcset", "sizes", "loading", "decoding",
        "data-recalc-dims", "data-attachment-id", "data-permalink",
        "data-orig-file", "data-orig-size", "data-comments-opened",
        "data-image-meta", "data-image-title", "data-image-description",
        "data-medium-file", "data-large-file", "data-id", "data-link",
        "data-url", "data-shortcode", "data-jetpack-boost",
        "fetchpriority",
    }

    def __init__(self, image_manifest):
        super().__init__(convert_charrefs=False)
        self.out = []
        self.image_manifest = image_manifest
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.DROP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        # Rewrite images
        if tag == "img":
            attrs_d = dict(attrs)
            src = attrs_d.get("src", "")
            new_src, rel = rewrite_url_to_local(src, drop_if_missing=True)
            if new_src is None:
                # Image isn't in the WP media library and can't be downloaded.
                # Drop the tag entirely rather than leave a broken-image icon.
                return
            attrs_d["src"] = new_src
            if rel:
                self.image_manifest.add(rel)
            # Strip cruft
            for k in list(attrs_d.keys()):
                if k in self.DROP_ATTRS or k.startswith("data-"):
                    attrs_d.pop(k, None)
            # Default alt to ""
            attrs_d.setdefault("alt", "")
            attrs_d.setdefault("loading", "lazy")
            self.out.append("<img " + self._render_attrs(attrs_d) + ">")
            return

        # Rewrite anchor hrefs that point to wp-content/uploads (rare lightbox links)
        if tag == "a":
            attrs_d = dict(attrs)
            href = attrs_d.get("href", "")
            new_href, rel = rewrite_url_to_local(href)
            if rel:
                attrs_d["href"] = new_href
                self.image_manifest.add(rel)
            # Rewrite internal blog links so they point to local pages.
            # Accept titaniablesh.com OR the old staging IP. The href may be
            # followed by query string, fragment, or stray junk like "(opens in
            # a new tab)" that ended up pasted into the URL field — match the
            # slug greedily and ignore whatever follows.
            m = re.match(
                r"^https?://(?:www\.)?(?:titaniablesh\.com|34\.76\.43\.69)/([a-z0-9-]+)",
                href,
                re.I,
            )
            if m:
                attrs_d["href"] = m.group(1) + ".html"
            for k in list(attrs_d.keys()):
                if k in self.DROP_ATTRS or k.startswith("data-"):
                    attrs_d.pop(k, None)
            self.out.append(f"<{tag} " + self._render_attrs(attrs_d) + ">")
            return

        # Strip class / style cruft from blocks; keep semantic tags only
        attrs_d = dict(attrs)
        for k in list(attrs_d.keys()):
            if k in self.DROP_ATTRS or k.startswith("data-"):
                attrs_d.pop(k, None)
        # Drop most class names — too tied to WP themes.
        attrs_d.pop("class", None)
        attrs_d.pop("style", None)
        attrs_d.pop("id", None)
        if attrs_d:
            self.out.append(f"<{tag} " + self._render_attrs(attrs_d) + ">")
        else:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in self.DROP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in self.SELF_CLOSING:
            return
        self.out.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        # treat as start tag; SELF_CLOSING list handles emitting only opening
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self._skip_depth:
            return
        self.out.append(data)

    def handle_entityref(self, name):
        if self._skip_depth:
            return
        self.out.append(f"&{name};")

    def handle_charref(self, name):
        if self._skip_depth:
            return
        self.out.append(f"&#{name};")

    def handle_comment(self, data):
        # Drop WP block-editor comments like <!-- wp:paragraph -->
        return

    @staticmethod
    def _render_attrs(d):
        parts = []
        for k, v in d.items():
            if v is None:
                parts.append(k)
            else:
                parts.append(f'{k}="{html.escape(v, quote=True)}"')
        return " ".join(parts)


def clean_post_content(raw_html, image_manifest):
    # WordPress's classic editor stores posts with implicit auto-paragraph: double
    # newlines should become <p>, single newlines inside text should become <br>.
    # We faithfully port wpautop() from WordPress core, then clean up tags/attrs.
    raw_html = _wpautop(raw_html)
    raw_html = _normalize_external_images(raw_html, image_manifest)
    raw_html = _apply_renames(raw_html)
    p = CleanParser(image_manifest)
    p.feed(raw_html)
    out = "".join(p.out)
    # collapse stray empties / runaway nesting
    out = re.sub(r"<p>\s*</p>", "", out)
    out = re.sub(r"<p>(\s*<p>)+", "<p>", out)
    out = re.sub(r"(</p>\s*)+</p>", "</p>", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    # Editorial decoration pass — adds classes that CleanParser stripped, plus
    # emoji on section headers and structural treatments.
    out = _decorate(out)
    return out.strip()


# ─────────────────────── editorial decoration ────────────────────────────
# These patterns are detected in the cleaned post HTML and rewritten into
# styled treatments. Order matters:
#   1. Callouts first   — consume specific labelled paragraphs
#   2. Pull quotes      — consume remaining standalone bold/italic paragraphs
#   3. Section emojis   — prefix h5 (and bare-strong-only h6) with context emoji
#   4. Drop cap         — applied last so it doesn't land inside a callout/quote

# (label_pattern, css_class, emoji, display_label)
CALLOUT_RULES = [
    (r"Pro\s*tip|PRO\s*TIP", "callout-tip", "💡", "PRO TIP"),
    (r"Suggerimento", "callout-tip", "💡", "SUGGERIMENTO"),
    (r"Tip", "callout-tip", "💡", "TIP"),
    (r"Attenzione", "callout-warning", "⚠️", "ATTENZIONE"),
    (r"Importante", "callout-warning", "❗", "IMPORTANTE"),
    (r"Avvertenza|Avvertimento", "callout-warning", "⚠️", "AVVERTENZA"),
    (r"Errore", "callout-warning", "🚨", "ERRORE COMUNE"),
    (r"Esempio", "callout-example", "✏️", "ESEMPIO"),
    (r"Curiosità", "callout-example", "✨", "CURIOSITÀ"),
    (r"Nota|N\.B\.", "callout-note", "📝", "NOTA"),
    (r"Conclusione", "callout-note", "🎯", "CONCLUSIONE"),
    (r"Ricorda(?:te)?", "callout-note", "🔖", "RICORDA"),
]


def _convert_callouts(html_str):
    """Wrap labelled paragraphs in styled callout boxes.

    Source posts use any of these styles (often within the same article):
      1. <p><strong>Label:</strong> content</p>      (label-only bold)
      2. <p><strong>Label: content</strong></p>      (entire line bold)
      3. <p><i>Label: content</i></p>                (entire line italic — used for "Esempio:" lines)
      4. <p><i>Label:</i> content</p>                (label-only italic)
      5. <p>Label: content</p>                       (no emphasis — bare)
    """
    EMPH = r"strong|b|em|i"
    for pat, klass, emoji, label in CALLOUT_RULES:
        regexes = [
            # Style 1: <p><emph>Label:</emph> content</p>
            re.compile(
                rf"<p>\s*<(?P<t>{EMPH})>\s*(?:{pat})\s*[:\-–—.]\s*</(?P=t)>\s*(.+?)</p>",
                re.IGNORECASE | re.DOTALL,
            ),
            # Style 2: <p><emph>Label: content</emph></p>
            re.compile(
                rf"<p>\s*<(?P<t>{EMPH})>\s*(?:{pat})\s*[:\-–—.]\s*(.+?)</(?P=t)>\s*</p>",
                re.IGNORECASE | re.DOTALL,
            ),
        ]

        def make_repl(_emoji, _label, _klass):
            def repl(m):
                # Pick the actual content group — last group is the body in both regexes
                inner = m.group(m.lastindex).strip()
                inner = re.sub(r"^[\s:–\-—]+", "", inner)
                # Some bodies may still contain the emphasis tag remnants — leave them.
                return (
                    f'<aside class="callout {_klass}">'
                    f'<p class="callout-label"><span class="callout-emoji">{_emoji}</span>'
                    f'<span class="callout-text">{_label}</span></p>'
                    f'<p class="callout-body">{inner}</p>'
                    f'</aside>'
                )
            return repl

        repl = make_repl(emoji, label, klass)
        for rx in regexes:
            html_str = rx.sub(repl, html_str)
    return html_str


PULL_QUOTE_RE = re.compile(
    r"<p>\s*<(?P<tag>strong|b|em|i)>(?P<text>[^<]{40,260})</(?P=tag)>\s*</p>",
    re.DOTALL,
)


def _convert_pullquotes(html_str, max_per_post=2):
    """Promote at most `max_per_post` standalone bold/italic paragraphs to
    pull-quote styling. Skip the first 600 characters of the body so we don't
    convert the lede paragraph (which gets the drop cap), and skip lines
    that look like list intros (end with colon) or category labels (start
    with capital + parenthetical, e.g. "Hard Magic (rule-based...)")."""
    pieces = []
    last = 0
    converted = 0
    for m in PULL_QUOTE_RE.finditer(html_str):
        if converted >= max_per_post:
            break
        if m.start() < 600:
            continue
        text = m.group("text").strip()
        if len(text.split()) < 6:
            continue
        # Skip list intros — they usually end with a colon and the next thing
        # in the source is a <ul>/<ol> starting up.
        if text.rstrip().endswith(":"):
            continue
        # Skip category labels like "Hard Magic (definition)" — short
        # parenthetical at the end is a strong signal it's labelling.
        if re.search(r"\([^)]{3,40}\)\s*$", text):
            continue
        pieces.append(html_str[last:m.start()])
        pieces.append(
            f'<blockquote class="pullquote"><p>{text}</p></blockquote>'
        )
        last = m.end()
        converted += 1
    pieces.append(html_str[last:])
    return "".join(pieces)


# Section-header emoji map — keyword in the heading text → emoji prefix.
SECTION_EMOJI_RULES = [
    (r"\b(prima\s+bozza|stesura|bozza)\b", "📝"),
    (r"\b(pianificaz|pianifica|outline|scaletta|struttura)\b", "🗺️"),
    (r"\b(personagg|character)\b", "👥"),
    (r"\b(idea|ispiraz|inspiration)\b", "💡"),
    (r"\b(scriv|writing)\b", "✍️"),
    (r"\b(magia|magic)\b", "🔮"),
    (r"\b(mond[oi]|world|ambient)\b", "🌍"),
    (r"\b(revisione|edit|correz|riscrittura)\b", "🔍"),
    (r"\b(pubblic|editor[ie]|edizione)\b", "📚"),
    (r"\b(attenzione|importante|errore|sbaglio|nemes)\b", "⚠️"),
    (r"\b(conclusione|riassunto|finale|fine)\b", "🎯"),
    (r"\b(esempi|caso|caso\s+studio)\b", "✏️"),
    (r"\b(curiosità|fun\s+fact)\b", "✨"),
    (r"\b(consigli|suggeriment|tips?)\b", "💡"),
    (r"\b(beta|lettore|reader)\b", "👀"),
    (r"\b(domande|question|FAQ)\b", "❓"),
    (r"\b(viaggio|hero|eroe)\b", "🦸"),
    (r"\b(fantasy)\b", "🐉"),
    (r"\b(fantascienza|sci-?fi)\b", "🚀"),
    (r"\b(genere|sottogener)\b", "📖"),
    (r"\b(blocco|writer'?s\s+block)\b", "🧱"),
    (r"\b(tastiera|keyboard|computer)\b", "⌨️"),
    (r"\b(tempo|time|durata|days?|giorni|settimana)\b", "⏱️"),
    (r"\b(idea\s+esplosiv|esplos)\b", "💥"),
    (r"\b(centrale|metà|middle)\b", "⚖️"),
    (r"\b(reverse|al\s+contrario)\b", "🔄"),
    (r"\b(motivazione|forza)\b", "💪"),
    (r"\b(proteg|tutela|copyright|diritti)\b", "🛡️"),
    (r"\b(soft\s+magic|hard\s+magic)\b", "🪄"),
    (r"\b(rubare|ladro|furto|copia)\b", "🦝"),
]


def _section_emoji(text):
    """Pick an emoji for a section-header text. Returns (emoji, stripped_text)."""
    plain = re.sub(r"<[^>]+>", "", text).strip()
    if not plain:
        return None, text
    # Don't double-emoji if the heading already starts with one.
    if re.match(r"^[\W_]*[\U0001F000-\U0001FFFF☀-➿]", plain):
        return None, text
    for pat, emoji in SECTION_EMOJI_RULES:
        if re.search(pat, plain, re.IGNORECASE):
            return emoji, text
    return "🌋", text  # default for unmatched headers — on-brand


def _decorate_section_headers(html_str):
    """Prefix h5 headers (and h6 headers that are pure bold text without inline
    images) with a context-appropriate emoji."""
    def repl(m):
        tag = m.group("tag")
        attrs = m.group("attrs") or ""
        body = m.group("body")
        # Skip h6 with inline images (those are already-decorated bullet rows)
        if tag.lower() == "h6" and "<img" in body.lower():
            return m.group(0)
        emoji, _ = _section_emoji(body)
        if not emoji:
            return m.group(0)
        return f'<{tag}{attrs}><span class="section-emoji">{emoji}</span> {body}</{tag}>'

    return re.sub(
        r"<(?P<tag>h[5-6])(?P<attrs>\s[^>]*)?>(?P<body>.*?)</(?P=tag)>",
        repl,
        html_str,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _add_drop_cap(html_str):
    """Wrap the first letter of the first substantive paragraph in a drop-cap span."""
    # Skip <p> elements that are inside <blockquote>, <aside>, <figure>, etc.,
    # or that contain only an image, by walking top-level <p> matches.
    pattern = re.compile(r"<p(?P<attrs>(?:\s[^>]*)?)>(?P<body>.*?)</p>", re.DOTALL)
    pieces = []
    last = 0
    applied = False
    for m in pattern.finditer(html_str):
        if applied:
            break
        # Only operate on plain <p> (no class), so we don't drop-cap a callout's body
        attrs = m.group("attrs") or ""
        if "class=" in attrs:
            continue
        body = m.group("body")
        plain = re.sub(r"<[^>]+>", "", body).strip()
        if len(plain) < 80:
            continue
        # Skip if the paragraph is inside a callout / blockquote / aside that
        # already opened above this point and hasn't closed.
        prefix = html_str[: m.start()]
        for open_tag, close_tag in (("<aside", "</aside>"), ("<blockquote", "</blockquote>")):
            if prefix.count(open_tag) > prefix.count(close_tag):
                break
        else:
            # Find the first letter, allowing inline tags & punctuation in front.
            cap_match = re.match(
                r"^(?P<prefix>(?:<[^>]+>|[^A-Za-zÀ-ſ])*)"
                r"(?P<letter>[A-Za-zÀ-ſ])"
                r"(?P<rest>.*)$",
                body,
                re.DOTALL,
            )
            if cap_match:
                new_body = (
                    f"{cap_match.group('prefix')}"
                    f"<span class=\"drop-cap\">{cap_match.group('letter')}</span>"
                    f"{cap_match.group('rest')}"
                )
                pieces.append(html_str[last:m.start()])
                pieces.append(f"<p{attrs}>{new_body}</p>")
                last = m.end()
                applied = True
                break
    pieces.append(html_str[last:])
    return "".join(pieces)


def _decorate(html_str):
    html_str = _convert_callouts(html_str)
    html_str = _convert_pullquotes(html_str)
    html_str = _decorate_section_headers(html_str)
    html_str = _add_drop_cap(html_str)
    return html_str


# Block-level element names that wpautop() treats as paragraph boundaries.
_BLOCK = (
    "table|thead|tfoot|caption|col|colgroup|tbody|tr|td|th|div|dl|dd|dt|"
    "ul|ol|li|pre|form|map|area|blockquote|address|math|style|p|"
    "h[1-6]|hr|fieldset|legend|section|article|aside|hgroup|header|footer|"
    "nav|figure|figcaption|details|menu|summary"
)


def _wpautop(text, br=True):
    """Faithful port of WordPress core's wpautop() — handles classic-editor posts
    that rely on implicit paragraph wrapping."""
    if not text or not text.strip():
        return ""

    # Standardize newlines
    text = re.sub(r"\r\n?", "\n", text)
    # Pad
    text = "\n" + text + "\n"

    # Add newlines around block elements
    text = re.sub(rf"(<(?:{_BLOCK})\b[^>]*>)", r"\n\1", text, flags=re.I)
    text = re.sub(rf"(</(?:{_BLOCK})>)", r"\1\n\n", text, flags=re.I)
    # <hr>, <br> are self-closing
    text = re.sub(r"(<hr\s*/?>)", r"\1\n\n", text, flags=re.I)

    # Collapse extra newlines
    text = re.sub(r"\n\n+", "\n\n", text)

    # Split into paragraphs
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    text = "\n".join(f"<p>{p.strip(chr(10))}</p>" for p in paragraphs)

    # Drop empties
    text = re.sub(r"<p>\s*</p>", "", text)
    # Unwrap <p> around block elements (keep nested block intact)
    text = re.sub(
        rf"<p>\s*(</?(?:{_BLOCK})\b[^>]*>)\s*</p>",
        r"\1",
        text,
        flags=re.I,
    )
    # Unwrap accidentally wrapped images-only paragraphs? leave them alone — they're fine.
    # Drop opening <p> that immediately precedes a block element
    text = re.sub(
        rf"<p>(\s*<(?:{_BLOCK})\b[^>]*>)",
        r"\1",
        text,
        flags=re.I,
    )
    # Drop closing </p> that immediately follows a block close
    text = re.sub(
        rf"(</(?:{_BLOCK})>\s*)</p>",
        r"\1",
        text,
        flags=re.I,
    )

    # Convert remaining single newlines to <br>
    if br:
        # Don't add <br> right before/after block tags
        text = re.sub(r"\n(?=\s*<(?:" + _BLOCK + r")\b)", " ", text, flags=re.I)
        text = re.sub(r"(</(?:" + _BLOCK + r")>)\s*\n", r"\1", text, flags=re.I)
        # Replace remaining \n with <br>
        text = re.sub(r"(?<!<br>)\n", "<br>\n", text)

    # Final empty-p sweep
    text = re.sub(r"<p>\s*(<br>\s*)*</p>", "", text)
    return text


# ───────────────────────────── HTML templates ────────────────────────────
SHARED_HEAD = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="icon" type="image/png" href="../assets/images/favicon.png">
<link rel="apple-touch-icon" href="../assets/images/favicon.png">
<link rel="stylesheet" href="../assets/fonts/fonts.css">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Lato:ital,wght@0,400;0,700;1,400;1,700&display=swap">
<link rel="stylesheet" href="blog.css">
</head>
<body>
"""

NAV_HTML = """\
<nav class="masthead volcanic">
  <a class="brand" href="../index.html">
    <span class="brand-logo">
      <img src="../assets/images/logo-animation.gif" alt="Volcano logo">
    </span>
    <span class="brand-text">Titania Blesh</span>
  </a>
  <ul class="nav-links" id="navlinks">
    <li><a href="../index.html#bestsellers">Books</a></li>
    <li><a href="../index.html#about">The author</a></li>
    <li><a href="index.html" class="current">Blog</a></li>
    <li><a href="../volcanoes.html">Volcanoes</a></li>
  </ul>
  <a class="nav-cta" href="http://www.amazon.it/dp/B0FRXS566B" target="_blank" rel="noopener">Buy the Books</a>
  <button class="nav-burger" id="burger" aria-label="Toggle menu">
    <span></span><span></span><span></span>
  </button>
</nav>
"""

FOOTER_HTML = """\
<footer class="volcanic">
  <p class="foot-left">© 2026 Titania Blesh</p>
  <ul class="foot-links">
    <li><a href="https://www.instagram.com/titaniablesh/" target="_blank">Instagram</a></li>
    <li><a href="https://www.tiktok.com/@titaniablesh" target="_blank">TikTok</a></li>
    <li><a href="#top">Top ↑</a></li>
  </ul>
</footer>
<script>
  const burger = document.getElementById('burger');
  const navlinks = document.getElementById('navlinks');
  if (burger && navlinks) {
    burger.addEventListener('click', () => navlinks.classList.toggle('open'));
    navlinks.querySelectorAll('a').forEach(a => a.addEventListener('click', () => navlinks.classList.remove('open')));
  }
</script>
</body></html>
"""


# Italian month names for nicely formatted dates
IT_MONTHS = [
    "", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
]


def fmt_date_it(date_raw):
    if not date_raw:
        return ""
    try:
        dt = datetime.strptime(date_raw[:19], "%Y-%m-%d %H:%M:%S")
        return f"{dt.day} {IT_MONTHS[dt.month]} {dt.year}"
    except Exception:
        return date_raw[:10]


def make_excerpt(post, length=180):
    """Plain-text excerpt from cleaned content."""
    if post.get("excerpt"):
        text = re.sub(r"<[^>]+>", "", post["excerpt"]).strip()
        if text:
            return _trim(text, length)
    raw = post["content"]
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return _trim(text, length)


def _trim(s, n):
    if len(s) <= n:
        return s
    s = s[:n]
    return s.rsplit(" ", 1)[0] + "…"


# ───────────────────────────── render: index ─────────────────────────────
def render_index(posts):
    by_cat = defaultdict(list)
    for p in posts:
        for c in p["categories"]:
            by_cat[c].append(p)
    for c in by_cat:
        by_cat[c].sort(key=lambda p: p["date"])

    sections = []
    for xml_cat, display in CATEGORY_ORDER:
        items = by_cat.get(xml_cat, [])
        if not items:
            continue
        lis = "\n".join(
            f'      <li><a href="{p["slug"]}.html">{html.escape(p["title"])}</a></li>'
            for p in items
        )
        sections.append(
            f'  <div class="cat-block">\n'
            f'    <h2 class="cat-title">{html.escape(display)}</h2>\n'
            f'    <ul class="cat-list">\n{lis}\n    </ul>\n'
            f'  </div>'
        )
    cat_html = "\n".join(sections)

    recent = sorted(posts, key=lambda p: p["date"], reverse=True)[:4]
    recent_cards = []
    for p in recent:
        excerpt = make_excerpt(p, 220)
        recent_cards.append(
            f'  <article class="post-card">\n'
            f'    <p class="post-card-meta">{fmt_date_it(p["date"])} · '
            f'{html.escape(p["categories"][0] if p["categories"] else "")}</p>\n'
            f'    <h3 class="post-card-title"><a href="{p["slug"]}.html">{html.escape(p["title"])}</a></h3>\n'
            f'    <p class="post-card-excerpt">{html.escape(excerpt)}</p>\n'
            f'    <a class="post-card-link" href="{p["slug"]}.html">Continua a leggere →</a>\n'
            f'  </article>'
        )
    recent_html = "\n".join(recent_cards)

    head = SHARED_HEAD.format(
        title="Blog · Titania Blesh",
        description="Appunti, esperienze e tecniche di scrittura — narrativa di genere, fantasy, fantascienza.",
    )
    body = f"""{head}{NAV_HTML}
<main id="top">

<header class="blog-hero">
  <p class="hero-eyebrow">Il blog</p>
  <h1 class="hero-title">Appunti di <em>scrittura</em></h1>
  <div class="hero-rule"><span class="line"></span><span class="star">✦</span><span class="line"></span></div>
  <blockquote class="hero-quote">
    <p><em>Writing is not about inspiration.<br>
    Writing is not about ideas.<br>
    Writing is not about luck.<br>
    Writing is about skill.</em></p>
    <cite>— Brandon Sanderson</cite>
  </blockquote>
  <div class="hero-prose">
    <p>Un tempo ero convinta che la scrittura fosse un talento innato, una vocazione, la chiamata dell'ispirazione. Tutte doti che io non ho mai avuto.</p>
    <p><strong>Scrivere è molto di più</strong>: è studio, dedizione, sacrificio, forza di volontà. È seguire delle regole. Quando ho iniziato a <em>studiare</em>, è cambiato tutto. E ho trovato un editore. Beh, più di uno.</p>
    <p>Questo blog vuole essere un archivio di tutto quello che ho imparato sulla scrittura, dalle conoscenze generali alle mie esperienze e disavventure personali. È un modo per avere tutti i miei appunti in un'unica raccolta ordinata e di condividerli con chi ha piacere di leggerli e di imparare insieme a me, giorno dopo giorno.</p>
  </div>
  <a class="hero-cta" href="#recent">Vai all'ultimo articolo ↓</a>
</header>

<section class="cat-grid">
  <h2 class="section-eyebrow">Indice degli articoli</h2>
{cat_html}
</section>

<section class="recent" id="recent">
  <h2 class="section-eyebrow">Articoli recenti</h2>
  <div class="recent-grid">
{recent_html}
  </div>
</section>

</main>
{FOOTER_HTML}"""
    return body


# ───────────────────────────── render: post ──────────────────────────────
def render_post(post, prev_post, next_post, image_manifest):
    cleaned = clean_post_content(post["content"], image_manifest)
    cat = post["categories"][0] if post["categories"] else ""
    head = SHARED_HEAD.format(
        title=f"{html.escape(post['title'])} · Titania Blesh",
        description=html.escape(make_excerpt(post, 160)),
    )
    nav_block = ""
    parts = []
    if prev_post:
        parts.append(
            f'    <a class="nav-prev" href="{prev_post["slug"]}.html">'
            f'<span class="nav-arrow">←</span>'
            f'<span class="nav-label">Articolo precedente</span>'
            f'<span class="nav-title">{html.escape(prev_post["title"])}</span></a>'
        )
    else:
        parts.append('    <span></span>')
    if next_post:
        parts.append(
            f'    <a class="nav-next" href="{next_post["slug"]}.html">'
            f'<span class="nav-arrow">→</span>'
            f'<span class="nav-label">Articolo successivo</span>'
            f'<span class="nav-title">{html.escape(next_post["title"])}</span></a>'
        )
    else:
        parts.append('    <span></span>')
    nav_block = '\n'.join(parts)

    body = f"""{head}{NAV_HTML}
<main id="top">

<article class="post">
  <header class="post-header">
    <p class="post-cat">{html.escape(cat)}</p>
    <h1 class="post-title">{html.escape(post["title"])}</h1>
    <p class="post-date">{fmt_date_it(post["date"])}</p>
    <div class="post-rule"><span class="line"></span><span class="star">✦</span><span class="line"></span></div>
  </header>

  <div class="post-body">
{cleaned}
  </div>

  <footer class="post-footer">
    <p class="back-link"><a href="index.html">← Torna all'indice del blog</a></p>
  </footer>
</article>

<nav class="post-nav">
{nav_block}
</nav>

</main>
{FOOTER_HTML}"""
    return body


# ───────────────────────────── shared CSS ────────────────────────────────
BLOG_CSS = """/* ─────────────────────────────────────────────────────────────
   Titania Blesh — blog stylesheet
   Sepia palette as the page default; volcanic masthead/footer.
   Reading-first: comfortable measure, generous leading, sober accents.
   ───────────────────────────────────────────────────────────── */

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:         #f5ede0;
  --bg-2:       #ece2cf;
  --bg-3:       #ddd0b6;
  --rule:       #ddd0b6;
  --rule-soft:  rgba(31,24,21,0.08);

  --txt:        #1f1815;
  --txt-mid:    #4a3d33;
  --txt-lt:     #8a7c6c;

  --accent:     #7a2e2e;
  --accent-dk:  #5b2020;
  --accent-lt:  #a14545;

  --gold:       #b88a3a;
  --gold-lt:    #d8b87a;

  --display:    'Lato', 'Nunito', system-ui, sans-serif;
  --body:       'Nunito', system-ui, -apple-system, sans-serif;
}

.volcanic {
  --bg:        #0d0908;
  --bg-2:      #14110f;
  --bg-3:      #1d1814;
  --rule:      rgba(240,230,212,0.16);
  --rule-soft: rgba(240,230,212,0.07);
  --txt:       #f0e6d4;
  --txt-mid:   #c5b89d;
  --txt-lt:    #847964;
  --accent:    #e85d2c;
  --accent-dk: #b8431b;
  --accent-lt: #f08855;
  --gold:      #d8a64a;
  --gold-lt:   #ebc77b;
}

html { scroll-behavior: smooth; scroll-padding-top: 6rem; }
body {
  background: var(--bg);
  color: var(--txt);
  font-family: var(--body);
  font-size: 17px;
  line-height: 1.7;
  overflow-x: hidden;
}
img { display: block; max-width: 100%; height: auto; }
a { color: inherit; }

/* ─── NAV (volcanic, lifted from index.html) ─────────────── */
nav.masthead {
  position: sticky; top: 0; z-index: 100;
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 1.5rem;
  padding: 1.4rem 3rem 1.2rem;
  background-color: var(--bg);
  background-image: linear-gradient(to right,
    rgba(240,230,212,0)   0%, rgba(240,230,212,0.15) 8%, rgba(240,230,212,0.55) 16%,
    rgba(240,230,212,0.92) 24%, rgba(240,230,212,1)    32%, rgba(240,230,212,1)    68%,
    rgba(240,230,212,0.92) 76%, rgba(240,230,212,0.55) 84%, rgba(240,230,212,0.15) 92%,
    rgba(240,230,212,0)   100%);
  background-position: bottom; background-size: 100% 2px; background-repeat: no-repeat;
}
.brand { grid-column: 1; display: flex; align-items: center; gap: 0.7rem; text-decoration: none; }
.brand-logo {
  position: relative; display: inline-flex; align-items: center; justify-content: center;
  width: 44px; height: 44px; flex-shrink: 0;
}
.brand-logo::before {
  content: ''; position: absolute; inset: -10px; border-radius: 50%;
  background: radial-gradient(circle at 50% 50%,
    rgba(255,230,210,0.92) 0%, rgba(248,150,100,0.70) 22%, rgba(232,93,44,0.42) 40%,
    rgba(232,93,44,0.22) 58%, rgba(232,93,44,0.10) 74%, rgba(232,93,44,0.03) 88%, rgba(232,93,44,0) 100%);
  z-index: 0; filter: blur(3px);
  animation: halo-pulse 4s ease-in-out infinite alternate;
}
@keyframes halo-pulse {
  from { opacity: 0.75; transform: scale(0.96); }
  to   { opacity: 1;    transform: scale(1.06); }
}
.brand-logo img { position: relative; z-index: 1; height: 32px; width: auto; transform: translateY(-6px); }
.brand-text {
  font-family: 'Lato', var(--body), system-ui, sans-serif;
  text-transform: uppercase; font-size: 1rem; color: var(--txt); letter-spacing: 0;
}
.nav-links {
  display: flex; gap: 2.2rem; list-style: none; align-items: center;
  justify-content: center; grid-column: 2;
}
.nav-links a {
  font-family: var(--body); font-size: 0.74rem; font-weight: 700;
  letter-spacing: 0.18em; text-transform: uppercase;
  color: var(--txt-mid); text-decoration: none;
  padding-bottom: 2px; border-bottom: 1.5px solid transparent;
  transition: color 0.2s, border-color 0.2s;
}
.nav-links a:hover, .nav-links a.current {
  color: var(--accent); border-bottom-color: var(--accent);
}
.nav-cta {
  grid-column: 3;
  font-family: var(--body); font-size: 0.7rem; font-weight: 700;
  letter-spacing: 0.18em; text-transform: uppercase;
  text-decoration: none;
  background-color: var(--accent); color: var(--bg);
  border: 1.5px solid var(--accent);
  padding: 0.55rem 1.1rem;
  transition: background 0.2s, color 0.2s, transform 0.15s, border-color 0.2s;
  white-space: nowrap;
}
.nav-cta:hover { background-color: transparent; color: var(--accent); transform: translateY(-1px); }
.nav-burger { display: none; flex-direction: column; gap: 5px; cursor: pointer; background: none; border: none; padding: 4px; }
.nav-burger span { display: block; width: 22px; height: 2px; background: var(--txt); border-radius: 2px; transition: all 0.3s; }

/* ─── BLOG HERO ────────────────────────────────── */
.blog-hero {
  text-align: center;
  padding: 5rem 1.5rem 4rem;
  max-width: 720px;
  margin: 0 auto;
  border-bottom: 1px solid var(--rule);
}
.hero-eyebrow {
  font-family: var(--body); font-size: 0.72rem; font-weight: 700;
  letter-spacing: 0.32em; text-transform: uppercase;
  color: var(--accent); margin-bottom: 1.2rem;
}
.hero-title {
  font-family: var(--display); font-style: italic; font-weight: 700;
  font-size: clamp(2.4rem, 7vw, 4.4rem);
  line-height: 1.05; letter-spacing: -0.015em;
  margin-bottom: 1rem;
}
.hero-title em { color: var(--accent); font-style: italic; }
.hero-rule {
  display: flex; align-items: center; justify-content: center;
  gap: 0.9rem; margin: 1.4rem 0 2rem;
}
.hero-rule .line { flex: 0 0 4rem; height: 1px; background: var(--txt); }
.hero-rule .star { color: var(--gold); font-size: 1rem; }
.hero-quote {
  font-family: var(--display); font-style: italic;
  color: var(--txt-mid);
  border-left: 3px solid var(--gold);
  padding: 0.4rem 0 0.4rem 1.4rem;
  margin: 0 auto 2.4rem;
  max-width: 28rem;
  text-align: left;
}
.hero-quote cite {
  display: block; margin-top: 0.6rem;
  font-style: normal; font-weight: 700;
  font-size: 0.78rem; letter-spacing: 0.15em; text-transform: uppercase;
  color: var(--accent);
}
.hero-prose p { margin-bottom: 1.2rem; color: var(--txt-mid); text-align: left; }
.hero-prose strong { color: var(--txt); font-weight: 700; }
.hero-prose em { font-style: italic; }
.hero-cta {
  display: inline-block; margin-top: 1rem;
  font-family: var(--body); font-size: 0.72rem; font-weight: 700;
  letter-spacing: 0.2em; text-transform: uppercase;
  color: var(--accent); text-decoration: none;
  border-bottom: 1.5px solid var(--accent);
  padding-bottom: 2px;
  transition: opacity 0.2s, transform 0.15s;
}
.hero-cta:hover { opacity: 0.7; transform: translateY(-1px); }

/* ─── INDEX: CATEGORIES ───────────────────────── */
.cat-grid {
  max-width: 1100px; margin: 0 auto; padding: 5rem 1.5rem 4rem;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 2.5rem 3rem;
}
.section-eyebrow {
  grid-column: 1 / -1;
  font-family: var(--body); font-size: 0.7rem; font-weight: 700;
  letter-spacing: 0.28em; text-transform: uppercase;
  color: var(--accent); margin-bottom: 0.4rem;
  text-align: center;
}
.cat-block { padding: 0.4rem 0; }
.cat-title {
  font-family: var(--display); font-weight: 700; font-style: italic;
  font-size: 1.4rem;
  color: var(--txt);
  border-bottom: 2px solid var(--gold);
  padding-bottom: 0.5rem;
  margin-bottom: 1rem;
  letter-spacing: -0.01em;
}
.cat-list {
  list-style: none;
  display: flex; flex-direction: column; gap: 0.5rem;
}
.cat-list li {
  position: relative;
  padding-left: 1rem;
}
.cat-list li::before {
  content: '✦';
  position: absolute; left: 0; top: 0.05em;
  color: var(--gold); font-size: 0.75rem;
}
.cat-list a {
  text-decoration: none; color: var(--txt-mid);
  border-bottom: 1px solid transparent;
  transition: color 0.18s, border-color 0.18s;
}
.cat-list a:hover { color: var(--accent); border-bottom-color: var(--accent); }

/* ─── INDEX: RECENT POSTS ─────────────────────── */
.recent {
  background: var(--bg-2);
  border-top: 1px solid var(--bg-3);
  padding: 5rem 1.5rem 6rem;
}
.recent-grid {
  max-width: 1100px; margin: 2rem auto 0;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 2rem;
}
.post-card {
  background: var(--bg);
  border: 1px solid var(--bg-3);
  padding: 1.8rem 1.6rem;
  display: flex; flex-direction: column; gap: 0.7rem;
  transition: transform 0.18s, box-shadow 0.18s, border-color 0.18s;
}
.post-card:hover {
  transform: translateY(-3px);
  box-shadow: 0 8px 22px rgba(31,24,21,0.10);
  border-color: var(--gold-lt);
}
.post-card-meta {
  font-family: var(--body); font-size: 0.7rem; font-weight: 700;
  letter-spacing: 0.16em; text-transform: uppercase;
  color: var(--txt-lt);
}
.post-card-title {
  font-family: var(--display); font-style: italic; font-weight: 700;
  font-size: 1.35rem; line-height: 1.2;
}
.post-card-title a {
  text-decoration: none; color: var(--txt);
  transition: color 0.18s;
}
.post-card-title a:hover { color: var(--accent); }
.post-card-excerpt { color: var(--txt-mid); font-size: 0.95rem; line-height: 1.55; }
.post-card-link {
  font-family: var(--body); font-size: 0.7rem; font-weight: 700;
  letter-spacing: 0.18em; text-transform: uppercase;
  color: var(--accent); text-decoration: none;
  margin-top: auto;
  border-bottom: 1.5px solid transparent;
  align-self: flex-start;
  transition: border-color 0.18s;
}
.post-card-link:hover { border-bottom-color: var(--accent); }

/* ─── POST PAGE ──────────────────────────────── */
.post {
  max-width: 720px;
  margin: 0 auto;
  padding: 4rem 1.5rem 3rem;
}
.post-header { text-align: center; margin-bottom: 3rem; }
.post-cat {
  display: inline-block;
  font-family: var(--body); font-size: 0.7rem; font-weight: 700;
  letter-spacing: 0.22em; text-transform: uppercase;
  color: var(--accent);
  border: 1px solid var(--accent);
  padding: 0.35rem 0.9rem;
  margin-bottom: 1.4rem;
}
.post-title {
  font-family: var(--display); font-style: italic; font-weight: 700;
  font-size: clamp(2rem, 5.2vw, 3.2rem);
  line-height: 1.1; letter-spacing: -0.015em;
  color: var(--txt);
  margin-bottom: 1rem;
}
.post-date {
  font-family: var(--body); font-size: 0.74rem; font-weight: 700;
  letter-spacing: 0.18em; text-transform: uppercase;
  color: var(--txt-lt);
}
.post-rule {
  display: flex; align-items: center; justify-content: center;
  gap: 0.9rem; margin: 1.6rem auto 0; max-width: 200px;
}
.post-rule .line { flex: 1; height: 1px; background: var(--gold); opacity: 0.7; }
.post-rule .star { color: var(--gold); font-size: 1rem; }

.post-body { font-size: 1.05rem; color: var(--txt); }
.post-body p { margin-bottom: 1.4rem; }
.post-body p:last-child { margin-bottom: 0; }
.post-body h1, .post-body h2, .post-body h3, .post-body h4, .post-body h5, .post-body h6 {
  font-family: var(--display); font-weight: 700; font-style: italic;
  color: var(--txt); letter-spacing: -0.01em;
  margin: 2.4rem 0 1rem;
  line-height: 1.2;
}
.post-body h2 { font-size: 1.7rem; }
.post-body h3 { font-size: 1.4rem; }
.post-body h4 { font-size: 1.2rem; }
.post-body h5, .post-body h6 { font-size: 1.05rem; }
.post-body strong { font-weight: 700; color: var(--txt); }
.post-body em { font-style: italic; }
.post-body a {
  color: var(--accent); text-decoration: none;
  border-bottom: 1px solid var(--accent-lt);
  transition: color 0.18s, border-color 0.18s;
}
.post-body a:hover { color: var(--accent-dk); border-bottom-color: var(--accent-dk); }
.post-body ul, .post-body ol { margin: 0 0 1.4rem 1.4rem; }
.post-body li { margin-bottom: 0.4rem; }
.post-body ul li::marker { color: var(--gold); }
.post-body blockquote {
  margin: 1.6rem 0;
  padding: 0.6rem 0 0.6rem 1.4rem;
  border-left: 3px solid var(--gold);
  font-style: italic; color: var(--txt-mid);
}
.post-body img {
  margin: 1.8rem auto;
  border: 1px solid var(--rule);
  background: var(--bg-2);
  max-width: 100%;
  height: auto;
}
.post-body figure { margin: 2rem 0; text-align: center; }
.post-body figure img { margin: 0 auto; }
.post-body figcaption {
  font-family: var(--display); font-style: italic;
  font-size: 0.88rem; color: var(--txt-lt);
  margin-top: 0.6rem;
}
.post-body hr {
  border: 0;
  height: 1px;
  background: var(--rule);
  margin: 2.4rem auto;
  max-width: 60%;
}
.post-body pre, .post-body code {
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  font-size: 0.9em;
  background: var(--bg-2);
  border: 1px solid var(--rule);
}
.post-body code { padding: 0.1em 0.35em; }
.post-body pre { padding: 1rem; overflow-x: auto; margin-bottom: 1.4rem; }
.post-body pre code { background: none; border: none; padding: 0; }
.post-body table {
  width: 100%;
  border-collapse: collapse;
  margin-bottom: 1.4rem;
  border: 1px solid var(--rule);
}
.post-body th, .post-body td {
  padding: 0.6rem 0.9rem;
  text-align: left;
  border-bottom: 1px solid var(--rule);
}
.post-body th { background: var(--bg-2); font-weight: 700; }
.post-body iframe {
  max-width: 100%;
  margin: 1.8rem auto;
  display: block;
}

/* ─── DROP CAP ─────────────────────────────────── */
.post-body .drop-cap {
  float: left;
  font-family: var(--display);
  font-style: italic;
  font-weight: 700;
  font-size: 4.6rem;
  line-height: 0.85;
  margin: 0.32rem 0.55rem -0.2rem 0;
  color: var(--accent);
  /* subtle illumination behind the letter */
  background: linear-gradient(135deg, rgba(184,138,58,0.10) 0%, transparent 65%);
  padding: 0.05em 0.12em 0.06em 0.12em;
  border-radius: 4px;
}

/* ─── PULL QUOTE ────────────────────────────────── */
.post-body blockquote.pullquote {
  font-family: var(--display);
  font-style: italic;
  font-weight: 400;
  font-size: 1.55rem;
  line-height: 1.35;
  text-align: center;
  color: var(--accent);
  max-width: 32rem;
  margin: 2.6rem auto;
  padding: 1.6rem 1rem;
  border: 0;
  border-top: 1px solid var(--gold);
  border-bottom: 1px solid var(--gold);
  position: relative;
}
.post-body blockquote.pullquote p { margin: 0; }
.post-body blockquote.pullquote::before,
.post-body blockquote.pullquote::after {
  position: absolute;
  font-family: 'Lora', Georgia, serif;
  font-size: 3rem;
  line-height: 1;
  color: var(--gold);
  font-style: normal;
  pointer-events: none;
}
.post-body blockquote.pullquote::before { content: '\201C'; left: 0.4rem; top: 0.6rem; }
.post-body blockquote.pullquote::after  { content: '\201D'; right: 0.4rem; bottom: 0.4rem; }

/* ─── CALLOUTS ──────────────────────────────────── */
.post-body .callout {
  margin: 2rem 0;
  padding: 1.1rem 1.4rem 1rem;
  border-left: 4px solid var(--gold);
  border-radius: 0 6px 6px 0;
  background: rgba(184,138,58,0.06);
  position: relative;
}
.post-body .callout p { margin-bottom: 0; }
.post-body .callout .callout-label {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  margin-bottom: 0.6rem;
  font-family: var(--body);
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--gold);
}
.post-body .callout .callout-emoji {
  font-size: 1.05rem;
  line-height: 1;
  letter-spacing: 0;
  /* nudge emoji vertically to align with caps text */
  transform: translateY(-1px);
}
.post-body .callout .callout-body {
  color: var(--txt);
  font-size: 1rem;
}

/* tip — gold (the default) */
.post-body .callout-tip {
  border-left-color: var(--gold);
  background: rgba(184,138,58,0.08);
}
.post-body .callout-tip .callout-label { color: #946a1f; }

/* warning — wine */
.post-body .callout-warning {
  border-left-color: var(--accent);
  background: rgba(122,46,46,0.06);
}
.post-body .callout-warning .callout-label { color: var(--accent); }

/* example — sage */
.post-body .callout-example {
  border-left-color: #5a7a6a;
  background: rgba(90,122,106,0.06);
}
.post-body .callout-example .callout-label { color: #4a6b5b; }

/* note — paper */
.post-body .callout-note {
  border-left-color: var(--txt-mid);
  background: rgba(74,61,51,0.05);
}
.post-body .callout-note .callout-label { color: var(--txt-mid); }

/* ─── SECTION HEADER EMOJI ──────────────────────── */
.post-body .section-emoji {
  display: inline-block;
  font-style: normal;
  margin-right: 0.4rem;
  /* keep emoji color/weight independent of heading style */
  font-family: 'Apple Color Emoji', 'Segoe UI Emoji', 'Noto Color Emoji', sans-serif;
  font-weight: 400;
  /* Slightly smaller than the heading text so it reads as a marker */
  font-size: 0.85em;
  vertical-align: 0.05em;
}

.post-footer { margin-top: 3rem; padding-top: 1.4rem; border-top: 1px solid var(--rule); }
.back-link a {
  font-family: var(--body); font-size: 0.74rem; font-weight: 700;
  letter-spacing: 0.18em; text-transform: uppercase;
  color: var(--accent); text-decoration: none;
}
.back-link a:hover { color: var(--accent-dk); }

/* ─── POST NAV (prev/next) ────────────────────── */
.post-nav {
  max-width: 1100px; margin: 0 auto; padding: 0 1.5rem 5rem;
  display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem;
}
.post-nav .nav-prev, .post-nav .nav-next {
  display: flex; flex-direction: column; gap: 0.4rem;
  padding: 1.4rem 1.6rem;
  background: var(--bg-2);
  border: 1px solid var(--bg-3);
  text-decoration: none;
  color: var(--txt);
  transition: transform 0.18s, border-color 0.18s, box-shadow 0.18s;
}
.post-nav .nav-prev:hover, .post-nav .nav-next:hover {
  border-color: var(--gold-lt);
  box-shadow: 0 6px 18px rgba(31,24,21,0.08);
  transform: translateY(-2px);
}
.post-nav .nav-next { text-align: right; }
.post-nav .nav-arrow {
  font-family: var(--body); font-size: 1rem; color: var(--accent);
}
.post-nav .nav-label {
  font-family: var(--body); font-size: 0.66rem; font-weight: 700;
  letter-spacing: 0.22em; text-transform: uppercase; color: var(--txt-lt);
}
.post-nav .nav-title {
  font-family: var(--display); font-style: italic; font-weight: 700;
  font-size: 1.05rem; line-height: 1.3; color: var(--txt);
}

/* ─── FOOTER ─────────────────────────────────── */
footer {
  background:
    linear-gradient(to right,
      rgba(240,230,212,0)   0%, rgba(240,230,212,0.15) 8%, rgba(240,230,212,0.55) 16%,
      rgba(240,230,212,0.92) 24%, rgba(240,230,212,1)    32%, rgba(240,230,212,1)    68%,
      rgba(240,230,212,0.92) 76%, rgba(240,230,212,0.55) 84%, rgba(240,230,212,0.15) 92%,
      rgba(240,230,212,0) 100%) top / 100% 2px no-repeat,
    radial-gradient(ellipse at 50% 0%, rgba(232,93,44,0.10) 0%, transparent 60%),
    linear-gradient(180deg, var(--bg-2) 0%, var(--bg) 60%, #2a1610 100%);
  padding: 3rem 3rem 2.2rem;
  position: relative; overflow: hidden;
  display: grid; grid-template-columns: 1fr 1fr;
  align-items: center; gap: 1.5rem;
}
.foot-left {
  font-family: var(--body); font-size: 0.85rem;
  color: var(--txt-lt); letter-spacing: 0.04em;
}
.foot-links {
  display: flex; gap: 1.6rem; list-style: none;
  justify-content: flex-end; flex-wrap: wrap;
}
.foot-links a {
  font-family: var(--body); font-size: 0.7rem; font-weight: 700;
  letter-spacing: 0.18em; text-transform: uppercase;
  color: var(--txt-mid); text-decoration: none;
  transition: color 0.2s;
}
.foot-links a:hover { color: var(--accent); }

/* ─── RESPONSIVE ─────────────────────────────── */
@media (max-width: 960px) {
  nav.masthead { padding: 1rem 1.2rem; grid-template-columns: 1fr auto; }
  nav.masthead::after { left: 1.2rem; right: 1.2rem; }
  .nav-cta { display: none; }
  .nav-links {
    display: none; flex-direction: column;
    position: absolute; top: 100%; left: 0; right: 0;
    background: var(--bg); border-bottom: 2px solid var(--txt);
    padding: 1.2rem 1.5rem 1.6rem; gap: 1.1rem;
    grid-column: 1 / -1; justify-content: flex-start;
  }
  .nav-links.open { display: flex; }
  .nav-burger { display: flex; }
  .blog-hero { padding: 3.5rem 1.2rem 3rem; }
  .post { padding: 3rem 1.2rem 2rem; }
  .cat-grid { padding: 3.5rem 1.2rem 3rem; gap: 2rem; }
  .recent { padding: 3.5rem 1.2rem 4rem; }
  .post-nav { grid-template-columns: 1fr; padding: 0 1.2rem 4rem; }
  .post-nav .nav-next { text-align: left; }
  footer { grid-template-columns: 1fr; padding: 2.4rem 1.5rem; gap: 1.5rem; text-align: center; }
  .foot-links { justify-content: center; }
}

@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  .brand-logo::before { animation: none; }
}
"""


# ───────────────────────────── main ──────────────────────────────────────
def main():
    posts = parse_xml()
    print(f"Loaded {len(posts)} posts")

    image_manifest = set()

    # write css
    with open(os.path.join(BLOG_DIR, "blog.css"), "w") as f:
        f.write(BLOG_CSS)

    # render each post (need image manifest + prev/next lookups)
    posts_chrono = sorted(posts, key=lambda p: p["date"])
    for idx, p in enumerate(posts_chrono):
        prev_p = posts_chrono[idx - 1] if idx > 0 else None
        next_p = posts_chrono[idx + 1] if idx < len(posts_chrono) - 1 else None
        out = render_post(p, prev_p, next_p, image_manifest)
        with open(os.path.join(BLOG_DIR, p["slug"] + ".html"), "w") as f:
            f.write(out)

    # render index
    with open(os.path.join(BLOG_DIR, "index.html"), "w") as f:
        f.write(render_index(posts))

    # write image manifest
    sorted_imgs = sorted(image_manifest)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(
            {
                "count": len(sorted_imgs),
                "expected_paths": sorted_imgs,
                "instructions": (
                    "Drop the WordPress wp-content/uploads/ tree into "
                    "assets/images/blog/ — each path above is relative to that folder."
                ),
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    print(f"Wrote {len(posts_chrono)} post pages, blog/index.html, blog/blog.css")
    print(f"Image manifest: {len(sorted_imgs)} unique images expected → {MANIFEST_PATH}")


if __name__ == "__main__":
    main()
