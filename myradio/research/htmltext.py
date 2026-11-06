"""Plain-text extraction from untrusted HTML.

The extractor keeps only text a visitor could actually see. It drops ``<script>``, ``<style>``, comments,
templates, embedded objects and anything marked hidden by the usual means (the ``hidden`` attribute,
``aria-hidden``, inline CSS such as ``display:none`` or ``font-size:0``, and common "visually hidden"
class names). It cannot see rules that live in external style sheets, so text hidden that way is kept.

It is bounded in input size and nesting depth, and it fails closed: a page that exceeds a bound raises
``HtmlLimit`` instead of returning partial, possibly misleading text. The time limit is checked between 16 KB
slices of input, so it bounds the whole read but cannot interrupt one slice; every step taken inside a slice is
linear in the slice (nothing here may use a pattern that can backtrack badly, because a stuck pattern holds
the interpreter lock and would freeze every other thread of the station, the audio clock included).
"""
from __future__ import annotations

import json
import re
import time
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin

from .textutil import clean_text, clean_url, parse_date

MAX_INPUT_CHARS = 600_000
PARSE_SECONDS = 2.5
MAX_DEPTH = 400
MAX_BLOCKS = 400
MAX_BLOCK_CHARS = 3000
MAX_TEXT_CHARS = 60_000
MAX_JSONLD = 5
MAX_JSONLD_CHARS = 65_536
_CHUNK = 16_384

# Never visible text, in a page or in the HTML inside a feed entry.
_ALWAYS_SKIP = frozenset("script style noscript template svg math canvas iframe object embed applet audio video head".split())
# Page furniture that is dropped from whole pages but kept inside feed entries.
_PAGE_SKIP = frozenset("select option textarea button datalist dialog nav footer aside form menu map".split())
_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_BLOCK = frozenset("""
address article blockquote body br dd details div dl dt fieldset figcaption figure h1 h2 h3 h4 h5 h6 header hr
li main ol p pre section summary table tbody td tfoot th thead tr ul
""".split())
_HIDDEN_CLASSES = frozenset("hidden sr-only visually-hidden screen-reader-text d-none hide invisible display-none".split())
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)|(?:^|[;\s])font-size\s*:\s*0(?:px|pt|em|rem|%)?\s*(?:[;!]|$)|"
    r"(?:^|[;\s])opacity\s*:\s*0(?:\.0+)?\s*(?:[;!]|$)|text-indent\s*:\s*-\d{3,}|"
    r"(?:left|top|right|bottom)\s*:\s*-\d{4,}|clip\s*:\s*rect\(\s*0|clip-path\s*:\s*inset\(\s*(?:50|100)%|"
    r"(?:max-)?(?:height|width)\s*:\s*0(?:px|pt|em|rem|%)?\s*(?:[;!]|$)|color\s*:\s*transparent",
    re.IGNORECASE)
# Elements whose end tag may be left out ("p", "li", "dd", "dt", "tr", "td", "th", the table sections, "option"). A new sibling
# (or, for a paragraph, a block element) ends them, as in a browser; without that a long list of "<li>" or "<p>" with no end tags
# would count as thousands of levels of nesting and the page would be refused.
_INLINE = frozenset("a abbr b bdi bdo big cite code data dfn em font i kbd mark nobr q s samp small span strike strong sub sup time tt u var".split())
_TRANSPARENT = _INLINE | {"p"}                          # what an ending walk may pass through on its way to the element it ends
# (A table is left out on purpose: a browser in "quirks mode" keeps a table inside an open paragraph, and a paragraph that is hidden
# must stay hidden around it. Where the browsers differ, the reader keeps the element nested, which only ever hides more.)
_P_CLOSERS = frozenset("""address article aside blockquote center details dialog dir div dl fieldset figcaption figure footer form
h1 h2 h3 h4 h5 h6 header hgroup hr main menu nav ol p pre section summary ul""".split())
_CELLS, _SECTIONS = ("td", "th"), ("thead", "tbody", "tfoot")
_IMPLIED_END = {
    **{tag: (("p",),) for tag in _P_CLOSERS},
    "li": (("li",), ("p",)), "dd": (("dd", "dt"), ("p",)), "dt": (("dd", "dt"), ("p",)),
    "td": (_CELLS,), "th": (_CELLS,), "tr": (_CELLS, ("tr",)),
    "thead": (_CELLS, ("tr",), _SECTIONS), "tbody": (_CELLS, ("tr",), _SECTIONS), "tfoot": (_CELLS, ("tr",), _SECTIONS),
    "option": (("option",),), "optgroup": (("option",), ("optgroup",)),
}
_REACH = 32                                              # how far down the stack an implied end looks: bounded work per tag
_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "application/json", "application/xml", "text/xml")
_DATE_META = ("article:published_time", "og:published_time", "datepublished", "date", "pubdate", "publishdate",
              "dc.date.issued", "dc.date", "dcterms.date", "article:modified_time", "og:updated_time", "last-modified")


class HtmlLimit(ValueError):
    """The page was too large, too deeply nested or too slow to read safely."""


class HtmlDoc:
    __slots__ = ("title", "description", "site_name", "canonical", "published_ts", "date_precision", "blocks",
                 "feed_links", "text", "language")

    def __init__(self):
        self.title = ""
        self.description = ""
        self.site_name = ""
        self.canonical = None
        self.published_ts = None
        self.date_precision = None
        self.blocks: list = []
        self.feed_links: list = []
        self.text = ""
        self.language = ""


def _strip_css(style: str) -> str:
    """The style text without its comments and backslashes, in one pass.

    Comments are removed (not replaced by a space), so ``dis/**/play:none`` still reads as ``display:none``: a page
    gains nothing by splitting a word, and a hidden element is never mistaken for a visible one. A comment that is
    never closed hides the rest, as it does in a browser. This is deliberately a plain scan and not a regular
    expression: a pattern such as ``/\\*.*?\\*/`` takes time that grows with the square of the length when the style
    holds many ``/*`` and no ``*/``, and that stalled the whole station for minutes."""
    out, at = [], 0
    while True:
        start = style.find("/*", at)
        if start < 0:
            out.append(style[at:])
            break
        out.append(style[at:start])
        end = style.find("*/", start + 2)
        if end < 0:
            break
        at = end + 2
    return "".join(out).replace("\\", "")


def _hidden(attrs: dict) -> bool:
    if "hidden" in attrs or (attrs.get("aria-hidden") or "").strip().lower() == "true":
        return True
    if (attrs.get("type") or "").strip().lower() == "hidden":
        return True
    style = attrs.get("style")
    if style and _HIDDEN_STYLE.search(_strip_css(style)):
        return True
    classes = attrs.get("class")
    return bool(classes and _HIDDEN_CLASSES.intersection(classes.lower().split()))


class _Extractor(HTMLParser):
    def __init__(self, base_url: str, fragment: bool):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.fragment = fragment
        self.doc = HtmlDoc()
        self.stack: list = []          # [tag, skips, in_main]
        self.skip = 0
        self.main = 0
        self.run: list = []
        self.in_title = False
        self.in_ld = False
        self.ld: list = []
        self.ld_blocks: list = []
        self.h1 = ""
        self.h1_main = ""
        self.in_h1 = False
        self.first_time = None
        self.dates: dict = {}
        self.meta: dict = {}
        self.total = 0
        self.blocks_main: list = []
        self.blocks_all: list = []

    # -- block handling --
    def _flush(self):
        if not self.run:
            return
        text = clean_text("".join(self.run), MAX_BLOCK_CHARS)
        self.run = []
        if text and len(self.blocks_all) < MAX_BLOCKS and self.total < MAX_TEXT_CHARS:
            self.total += len(text)
            self.blocks_all.append(text)
            if self.main:
                self.blocks_main.append(text)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            self._meta(attrs)
            return
        if tag == "link":
            self._link(attrs)
            return
        if tag == "time" and "datetime" in attrs and not self.skip and self.first_time is None:
            self.first_time = attrs["datetime"]
        if tag in _VOID:
            if tag in _BLOCK and not self.skip:
                self._flush()
            return
        if tag == "html" and attrs.get("lang"):
            self.doc.language = clean_text(attrs["lang"], 12).lower()
        if tag == "script" and (attrs.get("type") or "").strip().lower() == "application/ld+json" and len(self.ld_blocks) < MAX_JSONLD:
            self.in_ld = True
            self.ld = []
        if (tag == "title" and not self.doc.title and any(entry[0] == "head" for entry in self.stack)
                and not any(entry[1] and entry[0] != "head" for entry in self.stack)):
            self.in_title = True                 # only the page's own title: not one tucked into hidden or embedded content
        self._imply_end(tag)
        if len(self.stack) >= MAX_DEPTH:
            raise HtmlLimit("The page is nested too deeply to read safely.")
        skips = tag in _ALWAYS_SKIP or (tag in _PAGE_SKIP and not self.fragment) or _hidden(attrs)
        is_main = tag in ("article", "main") and not skips
        if tag in _BLOCK and not self.skip:
            self._flush()
        if tag == "h1" and not self.skip and not self.h1:
            self.in_h1 = True
        self.stack.append([tag, skips, is_main])
        if skips:
            self.skip += 1
        if is_main:
            self.main += 1

    def handle_startendtag(self, tag, attrs):
        # Browsers ignore the slash on non-void elements, so '<div hidden/>' still opens a hidden div.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _VOID:
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                break
        else:
            return
        self._close(index)

    def _close(self, index: int) -> None:
        """Close the open element at ``index`` and everything opened inside it."""
        if self.stack[index][0] in _BLOCK and not self.skip:
            self._flush()                       # before popping, so the text is still counted as inside <article>/<main>
        while len(self.stack) > index:
            name, skips, is_main = self.stack.pop()
            if skips:
                self.skip -= 1
            if is_main:
                self.main -= 1
            if name == "title":
                self.in_title = False
            if name == "script" and self.in_ld:
                self.in_ld = False
                if self.ld:
                    self.ld_blocks.append("".join(self.ld)[:MAX_JSONLD_CHARS])
            if name == "h1":
                self.in_h1 = False

    def _imply_end(self, tag: str) -> None:
        """A start tag that ends an element whose end tag was left out (``<li>`` after ``<li>``, a ``<div>`` inside an open
        ``<p>``), so that sloppy but legal pages do not look deeply nested. Each phase looks for the nearest element it ends,
        passing only over inline elements and paragraphs, and never further than ``_REACH``.

        It must never make the reader show what a browser hides, so it stops at anything it does not know to be safe: a block
        element, and any element that is hidden. (A browser re-opens a hidden ``<b>`` around the next paragraph, so text after
        ``<p><b style="display:none">x<p>`` is still hidden there; stopping at the hidden element keeps it hidden here.)"""
        phases = _IMPLIED_END.get(tag)
        if not phases:
            return
        for names in phases:
            stack = self.stack
            for index in range(len(stack) - 1, max(-1, len(stack) - 1 - _REACH), -1):
                name, hidden = stack[index][0], stack[index][1]
                if name in names:
                    self._close(index)
                    break
                if hidden or name not in _TRANSPARENT:
                    break

    def handle_data(self, data):
        if self.in_ld:
            if sum(map(len, self.ld)) < MAX_JSONLD_CHARS:
                self.ld.append(data)
            return
        if self.in_title:
            self.doc.title = clean_text(self.doc.title + data, 300)
            return
        if self.skip:
            return
        if self.in_h1 and len(self.h1) < 300:
            self.h1 = clean_text(self.h1 + " " + data, 300)
            if self.main:
                self.h1_main = self.h1
        if len(self.run) < 2000:
            self.run.append(data)

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass

    # -- metadata --
    def _meta(self, attrs):
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or attrs.get("http-equiv") or "").strip().lower()
        content = attrs.get("content", "")
        if not key or not content:
            return
        if key in ("og:title", "twitter:title"):
            self.meta.setdefault("title", clean_text(content, 300))
        elif key in ("og:description", "description", "twitter:description"):
            self.meta.setdefault("description", clean_text(content, 600))
        elif key == "og:site_name":
            self.meta.setdefault("site_name", clean_text(content, 80))
        elif key == "og:url":
            self.meta.setdefault("canonical", content)
        elif key in _DATE_META:
            self.dates.setdefault(key, content)

    def _link(self, attrs):
        rel = (attrs.get("rel") or "").lower().split()
        href = attrs.get("href", "")
        if not href:
            return
        if "canonical" in rel:
            self.meta["canonical"] = href
        elif "alternate" in rel:
            kind = (attrs.get("type") or "").split(";")[0].strip().lower()
            if kind in _FEED_TYPES and len(self.doc.feed_links) < 8:
                target = _absolute(self.base_url, href) if self.base_url else None
                if target:
                    self.doc.feed_links.append((target, kind, clean_text(attrs.get("title", ""), 80)))

    def finish(self) -> HtmlDoc:
        self._flush()
        doc = self.doc
        main_text = " ".join(self.blocks_main)
        blocks = self.blocks_main if len(main_text) >= 200 else self.blocks_all
        doc.blocks = blocks
        doc.text = clean_text(" ".join(blocks), MAX_TEXT_CHARS)
        doc.title = clean_text(self.meta.get("title") or self.h1_main or doc.title or self.h1, 300)
        doc.description = self.meta.get("description", "")
        doc.site_name = self.meta.get("site_name", "")
        canonical = self.meta.get("canonical")
        doc.canonical = _absolute(self.base_url, canonical) if canonical and self.base_url else None
        self._publication(doc)
        return doc

    def _publication(self, doc):
        candidates = []
        for key in _DATE_META:
            if key in self.dates:
                candidates.append(self.dates[key])
        for block in self.ld_blocks:
            candidates.extend(_jsonld_dates(block))
        if self.first_time:
            candidates.append(self.first_time)
        for value in candidates:
            ts, precision = parse_date(value)
            if ts is not None:
                doc.published_ts, doc.date_precision = ts, precision
                return


def _absolute(base: str, href: str) -> Optional[str]:
    """``href`` made absolute against ``base`` and tidied, or ``None``. An address a page wrote badly (``http://[bad``)
    is simply not used: it must never make the rest of the page unreadable."""
    try:
        return clean_url(urljoin(base, href))
    except ValueError:
        return None


def _jsonld_dates(block: str) -> list:
    try:
        data = json.loads(block, parse_constant=lambda _: None)
    except (ValueError, RecursionError):
        return []
    found, budget = [], [400]

    def walk(node, depth):
        if depth > 6 or budget[0] <= 0 or len(found) >= 4:
            return
        budget[0] -= 1
        if isinstance(node, dict):
            for key in ("datePublished", "dateCreated", "dateModified"):
                value = node.get(key)
                if isinstance(value, str):
                    found.append(value)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node[:50]:
                walk(value, depth + 1)

    walk(data, 0)
    return found


def parse_html(html: str, *, base_url: str = "", fragment: bool = False, seconds: float = PARSE_SECONDS) -> HtmlDoc:
    """Extract visible text and metadata. ``fragment=True`` is for the HTML inside a feed entry."""
    if not isinstance(html, str):
        return HtmlDoc()
    if len(html) > MAX_INPUT_CHARS:
        html = html[:MAX_INPUT_CHARS]
    parser = _Extractor(base_url, fragment)
    started = time.monotonic()
    try:
        for index in range(0, len(html), _CHUNK):
            parser.feed(html[index:index + _CHUNK])
            if time.monotonic() - started > seconds:
                raise HtmlLimit("The page took too long to read safely.")
        parser.close()
        return parser.finish()
    except HtmlLimit:
        raise
    except RecursionError:
        raise HtmlLimit("The page is nested too deeply to read safely.") from None
    except Exception:
        raise HtmlLimit("The page could not be read.") from None


# Names of real HTML elements. Text is read as HTML only when it holds one of these, a comment or declaration, or a character
# reference; anything else (``List<String>``, ``Contact <info@club.example>``) is plain text and keeps its angle brackets.
_HTML_TAGS = frozenset("""
a abbr acronym address applet area article aside audio b base basefont bdi bdo big blockquote body br button canvas caption center
cite code col colgroup data datalist dd del details dfn dialog dir div dl dt em embed fieldset figcaption figure font footer form
frame frameset h1 h2 h3 h4 h5 h6 head header hgroup hr html i iframe img input ins kbd label legend li link main map mark math menu
meta meter nav nobr noframes noscript object ol optgroup option output p param picture pre progress q rp rt ruby s samp script
section select slot small source span strike strong style sub summary sup svg table tbody td template textarea tfoot th thead
time title tr track tt u ul var video wbr
""".split())
_MARKUP = re.compile(
    r"</?(?:" + "|".join(sorted(_HTML_TAGS, key=len, reverse=True)) + r")(?![A-Za-z0-9_:-])|<[!?]|"
    r"&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});", re.IGNORECASE)


_GATE_WINDOW = 16_384      # characters searched per call: one call on hostile text (``<`` x 16,384) holds the interpreter about 15 ms
_GATE_OVERLAP = 64         # longer than any match of ``_MARKUP`` (at most 35 characters with its look-ahead)


def looks_like_html(text) -> bool:
    """True when ``text`` holds HTML: a real element name, a comment, a declaration or a character reference.

    The text is searched in windows, so a feed field full of ``<`` (which the pattern tries about 110 element names at) cannot hold
    the interpreter lock for half a second while the playout clock waits: other threads run between windows. A match counts only
    if it starts inside its window proper; each search may look ``_GATE_OVERLAP`` characters further so that a match (and its
    look-ahead) is never cut off by the window edge. The answer is the same as one search over the first ``MAX_INPUT_CHARS``."""
    if not isinstance(text, str):
        return False
    end = min(len(text), MAX_INPUT_CHARS)      # the reader looks no further than this either
    for start in range(0, end, _GATE_WINDOW):
        found = _MARKUP.search(text, start, min(end, start + _GATE_WINDOW + _GATE_OVERLAP))
        if found is not None and found.start() < start + _GATE_WINDOW:
            return True
    return False


def readable_text(value, limit: int = 0) -> str:
    """The text of a field a feed or page filled in by hand. Real HTML is read as HTML (hidden parts are dropped and
    entities are decoded); anything else is plain text, so a title such as ``Using List<String> in Java`` is not damaged."""
    if not isinstance(value, str) or not value:
        return ""
    return html_to_text(value, limit) if looks_like_html(value) else clean_text(value, limit)


def html_to_text(fragment: str, limit: int = 0) -> str:
    """Visible text of an HTML fragment (a feed entry's summary). Falls back to tag-stripped text if the
    fragment trips a safety limit."""
    if not isinstance(fragment, str) or not fragment:
        return ""
    if "<" not in fragment and "&" not in fragment:
        return clean_text(fragment, limit)
    try:
        doc = parse_html(fragment, fragment=True, seconds=1.0)
    except HtmlLimit:
        return clean_text(re.sub(r"<[^>]{0,500}>", " ", fragment[:50_000]), limit)
    return clean_text(doc.text, limit)
