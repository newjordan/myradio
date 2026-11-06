"""Feed parsers: RSS 2.0 (plus RSS 0.9x and RSS 1.0/RDF), Atom 1.0 and JSON Feed 1.x, and a page reader.

Safety rules, all enforced here rather than hoped for:

* A document type declaration of any kind (``<!DOCTYPE ...>``) is a hard error. Entity declarations can only
  appear inside one, so no entity is ever defined, expanded or fetched. The refusal is made by looking at the start
  of the document *before the XML parser sees any of it* (a declaration can only stand ahead of the root element), so it
  does not depend on how a particular copy of the parser limits entity expansion; the parser's own refusal stays as a
  second layer. Predefined entities and character references still work, and an undefined entity is a parse error.
* Element count, nesting depth and text volume are bounded while parsing, not after.
* JSON is parsed with ``NaN``/``Infinity`` refused, big numbers neutralised and recursion errors caught.
* Every text field is passed through ``clean_text``. A field that holds real HTML (a known element, a comment or a character
  reference) is read as HTML first, with its hidden parts dropped; any other text, such as ``List<String>``, is kept as written.
"""
from __future__ import annotations

import codecs
import html as _html
import json
import re
import sys
from html import entities as html_entities
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

from .htmltext import HtmlDoc, HtmlLimit, html_to_text, parse_html, readable_text
from .textutil import clean_text, clean_url, parse_date

MAX_ELEMENTS = 60_000
MAX_DEPTH = 64
MAX_TEXT = 3_000_000
MAX_ENTRIES = 300
TITLE_CHARS, SUMMARY_CHARS, CONTENT_CHARS = 300, 3000, 6000
ATOM = "{http://www.w3.org/2005/Atom}"
RSS1 = "{http://purl.org/rss/1.0/}"
RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"


class FeedError(ValueError):
    """The document is not something the station can read. ``str(error)`` is plain language."""


@dataclass
class Entry:
    title: str = ""
    link: Optional[str] = None
    guid: str = ""
    published_ts: Optional[float] = None
    date_precision: Optional[str] = None
    date_basis: str = ""          # "published" or "updated"
    summary: str = ""
    content: str = ""
    categories: tuple = ()
    author: str = ""


@dataclass
class FeedDoc:
    format: str                    # "rss", "atom", "jsonfeed" or "rdf"
    title: str = ""
    link: Optional[str] = None
    entries: list = field(default_factory=list)
    truncated: bool = False


# ---- decoding and sniffing ---------------------------------------------------------------------

_DECODE_SLICE = 65_536
_SLICED_DECODERS = {"utf-16-le": codecs.utf_16_le_decode, "utf-16-be": codecs.utf_16_be_decode,
                    "utf-32-le": codecs.utf_32_le_decode, "utf-32-be": codecs.utf_32_be_decode}
_MARKS = {"utf-16": (b"\xff\xfe", b"\xfe\xff"), "utf-32": (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")}
_NATIVE = "-le" if sys.byteorder == "little" else "-be"


def _decode(body: bytes, name: str) -> str:
    """``body.decode(name, "replace")``. The UTF-16 and UTF-32 decoders take about 150 ms for 2 MB full of broken pairs, all of it
    holding the interpreter lock, so those are decoded a slice at a time and other threads run in between. The byte-order decoders
    used for that leave a cut pair for the next slice, so the text is the same; as with ``decode``, a byte order mark decides the order
    and without one this computer's own order is used. (Python's incremental ``utf-16`` decoder is not used: it refuses text with
    no byte order mark, which ``decode`` accepts.)"""
    key = codecs.lookup(name).name
    if len(body) <= _DECODE_SLICE or not key.startswith(("utf-16", "utf-32")):
        return body.decode(name, "replace")
    data = body
    if key in _MARKS:
        little, big = _MARKS[key]
        if data.startswith(little):
            key, data = key + "-le", data[len(little):]
        elif data.startswith(big):
            key, data = key + "-be", data[len(big):]
        else:
            key += _NATIVE
    step = _SLICED_DECODERS.get(key)
    if step is None:
        return body.decode(name, "replace")
    parts, at = [], 0
    while at < len(data):
        last = at + _DECODE_SLICE >= len(data)
        text, used = step(data[at:at + _DECODE_SLICE], "replace", last)
        parts.append(text)
        if last:
            break
        if used <= 0:                                  # cannot happen with slices this long; never loop on it
            parts.append(data[at:].decode(key, "replace"))
            break
        at += used
    return "".join(parts)


def decode_text(body: bytes, charset: str = "") -> str:
    """Bytes to text with a Byte Order Mark first, then the declared character set, then UTF-8."""
    if body.startswith(b"\xef\xbb\xbf"):
        return body[3:].decode("utf-8", "replace")
    if body.startswith((b"\xff\xfe", b"\xfe\xff")):
        return _decode(body, "utf-16")
    for name in (charset, "utf-8"):
        if not name:
            continue
        try:
            return _decode(body, name.strip().strip("\"'"))
        except (LookupError, ValueError):
            continue
    return body.decode("utf-8", "replace")


def sniff(body: bytes) -> str:
    """Rough document type from the first bytes: ``xml``, ``json``, ``html`` or ``text``.

    Anything with a document type declaration that is not an HTML one counts as XML, so that the XML reader (which
    refuses every such declaration) is the one to see it.
    """
    head = body[:4096]
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "xml"
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text.startswith((b"{", b"[")):
        return "json"
    low = text.decode("latin-1")
    if not low.startswith("<"):
        return "text"
    declared_xml = low.startswith("<?xml")
    if declared_xml:
        end = low.find("?>")
        low = low[end + 2:].lstrip() if end >= 0 else low[5:]
    if low.startswith("<!doctype"):
        return "html" if low[9:].lstrip().startswith("html") else "xml"
    found = [i for i in (low.find(m) for m in ("<rss", "<feed", "<rdf:rdf", "<rdf ")) if i >= 0]
    html_at = low.find("<html")
    if found and (html_at < 0 or min(found) < html_at):
        return "xml"
    if html_at >= 0:
        return "html"
    return "xml" if declared_xml else "html"


# ---- XML ---------------------------------------------------------------------------------------

DOCTYPE_MESSAGE = ("This feed starts with a kind of header that the station does not accept for safety reasons, so it was not read. "
                   "Look for the site's plain RSS or Atom feed address instead.")
_PROLOG_SPACE = re.compile(rb"(?:\xef\xbb\xbf|\xff\xfe|\xfe\xff|\s)*")
_PROLOG_WINDOW = 65_536


def _skip_prolog_space(text: bytes, at: int) -> int:
    """Where the blanks and byte order marks starting at ``at`` end. Matched a window at a time (2 MB of blanks in one match held
    the interpreter about 165 ms); a match that stops within two bytes of its window edge may have been cut there (a byte order
    mark is three bytes), so matching goes on from where it stopped."""
    while True:
        stop = at + _PROLOG_WINDOW
        end = _PROLOG_SPACE.match(text, at, stop).end()
        if end <= at or stop >= len(text) or end < stop - 2:
            return end
        at = end


def _has_declaration(body: bytes) -> bool:
    """True when the document opens with a ``<!DOCTYPE`` (or ``<!ENTITY``) declaration. Only the part ahead of the root element
    is looked at, because that is the only place such a declaration can stand: text inside the feed that merely contains the
    word (for example a CDATA section quoting an HTML page) is not a declaration. Text in UTF-16 or UTF-32 is looked at too."""
    text = body.replace(b"\x00", b"")
    at = _skip_prolog_space(text, 0)
    for _ in range(1000):
        if text.startswith(b"<?", at):
            end = text.find(b"?>", at + 2)
            width = 2
        elif text.startswith(b"<!--", at):
            end = text.find(b"-->", at + 4)
            width = 3
        elif text.startswith(b"<!", at):
            return True                         # <!DOCTYPE ...>, <!ENTITY ...> or any other declaration ahead of the root
        else:
            return False                        # the root element (or text the parser will reject on its own)
        if end < 0:
            return False
        at = _skip_prolog_space(text, end + width)
    return False


class _Target:
    """Tree builder with hard limits. Comments and processing instructions are dropped; any DOCTYPE is fatal."""

    def __init__(self):
        self._builder = ET.TreeBuilder()
        self._elements = self._depth = self._text = 0

    def start(self, tag, attrib):
        self._elements += 1
        self._depth += 1
        if self._elements > MAX_ELEMENTS or self._depth > MAX_DEPTH:
            raise FeedError("This feed is too large or too deeply nested to read safely.")
        return self._builder.start(tag, attrib)

    def end(self, tag):
        self._depth -= 1
        return self._builder.end(tag)

    def data(self, data):
        self._text += len(data)
        if self._text > MAX_TEXT:
            raise FeedError("This feed has too much text to read safely.")
        self._builder.data(data)

    def close(self):
        return self._builder.close()

    def doctype(self, name, pubid, system):
        raise FeedError(DOCTYPE_MESSAGE)


_NAMED_REFERENCE = re.compile(rb"&([A-Za-z][A-Za-z0-9]{1,31});")
_BARE_AMPERSAND = re.compile(rb"&(?!(?:[A-Za-z][A-Za-z0-9]{0,31}|#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6});)")
_ILLEGAL_XML = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_XML_NAMES = frozenset({b"amp", b"lt", b"gt", b"quot", b"apos"})


def _repair(body: bytes) -> Optional[bytes]:
    """The three most common real-world slips in otherwise fine feeds: blank lines before the XML declaration, HTML
    entity names XML does not define (``&nbsp;``, ``&copy;``), a bare ``&``, and control characters. Repairs only
    ever add numeric character references, never an entity definition, so nothing is expanded. Text in UTF-16 is left alone."""
    if body[:2] in (b"\xff\xfe", b"\xfe\xff") or body[:400].count(b"\x00") > 20:       # UTF-16 or UTF-32 text: left alone
        return None

    def named(match):
        name = match.group(1)
        code = None if name in _XML_NAMES else html_entities.name2codepoint.get(name.decode("ascii"))
        return b"&#%d;" % code if code else match.group(0)

    text = b"".join(_ILLEGAL_XML.sub(b"", _BARE_AMPERSAND.sub(b"&amp;", _NAMED_REFERENCE.sub(named, piece)))
                    for piece in _repair_pieces(body.lstrip(b" \t\r\n")))
    return text if text != body else None


_REPAIR_PIECE = 65_536


def _repair_pieces(text: bytes):
    """``text`` in pieces of about 64 KB for the repair patterns (one pass over 2 MB of ``&`` or control characters held the
    interpreter about 200 ms). A cut is made only where no reference can straddle it: just before an ``&`` (no reference contains
    one, and a look-ahead that reaches it fails there just as at a cut), or where the last 40 bytes hold none (a reference with its
    look-ahead is at most 35 bytes). So each piece is repaired exactly as the whole would be."""
    start, size = 0, len(text)
    while start < size:
        stop = start + _REPAIR_PIECE
        if stop >= size:
            yield text[start:]
            return
        amp = text.rfind(b"&", stop - 40, stop)
        if amp > start:
            stop = amp
        yield text[start:stop]
        start = stop


def _parse_once(body: bytes) -> ET.Element:
    parser = ET.XMLParser(target=_Target())
    for index in range(0, len(body), 65536):
        parser.feed(body[index:index + 65536])
    return parser.close()


def _parse_xml(body: bytes) -> ET.Element:
    problem = "This address does not contain a readable feed."
    if _has_declaration(body):
        raise FeedError(DOCTYPE_MESSAGE)
    try:
        return _parse_once(body)
    except FeedError:
        raise
    except (ET.ParseError, ValueError, LookupError, RecursionError, MemoryError):
        pass
    repaired = _repair(body)
    if repaired is None:
        raise FeedError(problem)
    try:
        return _parse_once(repaired)
    except FeedError:
        raise
    except (ET.ParseError, ValueError, LookupError, RecursionError, MemoryError):
        raise FeedError(problem) from None


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(node, *names):
    for child in node:
        if child.tag in names:
            return child
    return None


def _children(node, *names):
    return [child for child in node if child.tag in names]


def _text_of(node) -> str:
    if node is None:
        return ""
    return "".join(node.itertext())


def _inner_html(node) -> str:
    """The HTML an element carries. A leaf's text is HTML source (escaped inside the XML), so it is used as
    is; real child elements are rebuilt with plain names so hidden markup inside them is still recognised."""
    if node is None:
        return ""
    if len(node) == 0:
        return node.text or ""
    out = [_html.escape(node.text or "", quote=False)]

    def build(element):
        name = _local(element.tag)
        attrs = "".join(f' {_local(k)}="{_html.escape(str(v), quote=True)}"' for k, v in element.attrib.items())
        out.append(f"<{name}{attrs}>")
        out.append(_html.escape(element.text or "", quote=False))
        for child in element:
            build(child)
        out.append(f"</{name}>")
        out.append(_html.escape(element.tail or "", quote=False))

    for child in node:
        build(child)
    return "".join(out)


def _readable(node, limit: int = 0) -> str:
    """The text of an element. A leaf holding real HTML (escaped inside the XML) is read as HTML; a leaf holding anything
    else is plain text, so ``Using List<String> in Java`` is not damaged. Child elements (XHTML) are always HTML."""
    if node is None:
        return ""
    if len(node) == 0:
        return readable_text(node.text or "", limit)
    return html_to_text(_inner_html(node), limit)


def _title(node) -> str:
    return clean_text(_readable(node), TITLE_CHARS)


def _body(node, limit) -> str:
    return _readable(node, limit)


def _link(base: str, value) -> Optional[str]:
    """An absolute http(s) address from a feed's link text, resolving a relative one against the feed's own address."""
    if not isinstance(value, str):
        return None
    value = clean_text(value)
    if not value:
        return None
    try:
        return clean_url(urljoin(base, value) if base else value)
    except ValueError:
        return None


def _first_date(*values):
    for value in values:
        ts, precision = parse_date(value) if isinstance(value, str) else (None, None)
        if ts is not None:
            return ts, precision
    return None, None


def _entry_from_rss(item, base: str) -> Entry:
    published, precision = _first_date(_text_of(_child(item, "pubDate", "date")), _text_of(_child(item, DC + "date")),
                                       _text_of(_child(item, ATOM + "published")), _text_of(_child(item, ATOM + "updated")))
    guid_node = _child(item, "guid")
    link = _link(base, _text_of(_child(item, "link", RSS1 + "link")))
    if link is None and guid_node is not None and (guid_node.get("isPermaLink", "true").lower() != "false"):
        link = _link(base, _text_of(guid_node))
    categories = tuple(dict.fromkeys(clean_text(html_to_text(_text_of(c)), 60) for c in _children(item, "category", DC + "subject")[:8]
                                      if clean_text(_text_of(c))))
    return Entry(
        title=_title(_child(item, "title", RSS1 + "title")),
        link=link,
        guid=clean_text(_text_of(guid_node), 300),
        published_ts=published, date_precision=precision, date_basis="published" if published is not None else "",
        summary=_body(_child(item, "description", RSS1 + "description", "summary"), SUMMARY_CHARS),
        content=_body(_child(item, CONTENT + "encoded", "content"), CONTENT_CHARS),
        categories=categories,
        author=clean_text(_text_of(_child(item, DC + "creator", "author")), 80),
    )


_ATOM_HTML = frozenset(("html", "xhtml", "text/html", "application/xhtml+xml"))


def _atom_text(node, limit) -> str:
    """An Atom title, summary or content. ``type="html"`` and ``"xhtml"`` are HTML; ``"text"`` (the default) is plain text
    unless it plainly holds HTML anyway, which many feeds do."""
    if node is None:
        return ""
    if (node.get("type") or "text").strip().lower() in _ATOM_HTML:
        return html_to_text(_inner_html(node), limit)
    return _readable(node, limit)


def _entry_from_atom(entry, base: str) -> Entry:
    published_node, updated_node = _child(entry, ATOM + "published"), _child(entry, ATOM + "updated")
    ts, precision = _first_date(_text_of(published_node))
    basis = "published" if ts is not None else ""
    if ts is None:
        ts, precision = _first_date(_text_of(updated_node))
        basis = "updated" if ts is not None else ""
    link = None
    for node in _children(entry, ATOM + "link"):
        rel = (node.get("rel") or "alternate").lower()
        kind = (node.get("type") or "").lower()
        if rel == "alternate" and (not kind or "html" in kind):
            link = _link(base, node.get("href", ""))
            if link:
                break
    categories = tuple(dict.fromkeys(clean_text(c.get("label") or c.get("term") or "", 60) for c in _children(entry, ATOM + "category")[:8]
                                      if clean_text(c.get("label") or c.get("term") or "")))
    author = _child(entry, ATOM + "author")
    return Entry(
        title=_atom_text(_child(entry, ATOM + "title"), TITLE_CHARS),
        link=link, guid=clean_text(_text_of(_child(entry, ATOM + "id")), 300),
        published_ts=ts, date_precision=precision, date_basis=basis,
        summary=_atom_text(_child(entry, ATOM + "summary"), SUMMARY_CHARS),
        content=_atom_text(_child(entry, ATOM + "content"), CONTENT_CHARS),
        categories=categories,
        author=clean_text(_text_of(_child(author, ATOM + "name")) if author is not None else "", 80),
    )


def _strip_namespace(root, namespace: str) -> None:
    """Give every element of ``namespace`` its plain name, leaving elements of other namespaces (``dc:``, ``content:``) alone."""
    prefix = "{" + namespace + "}"
    for node in root.iter():
        if isinstance(node.tag, str) and node.tag.startswith(prefix):
            node.tag = node.tag[len(prefix):]


def _parse_xml_feed(body: bytes, base: str) -> FeedDoc:
    root = _parse_xml(body)
    if _local(root.tag) == "rss" and root.tag != "rss":
        _strip_namespace(root, root.tag[1:root.tag.index("}")])       # RSS 2.0 declared in a default namespace
    tag = root.tag
    doc: FeedDoc
    if tag == ATOM + "feed":
        link = None
        for node in _children(root, ATOM + "link"):
            if (node.get("rel") or "alternate").lower() == "alternate":
                link = _link(base, node.get("href", ""))
                break
        doc = FeedDoc("atom", _title(_child(root, ATOM + "title")), link)
        nodes = _children(root, ATOM + "entry")
        make = lambda node: _entry_from_atom(node, base)  # noqa: E731
    elif tag == "rss":
        channel = _child(root, "channel")
        if channel is None:
            raise FeedError("This address does not contain a readable feed.")
        doc = FeedDoc("rss", _title(_child(channel, "title")), _link(base, _text_of(_child(channel, "link"))))
        nodes = _children(channel, "item")
        make = lambda node: _entry_from_rss(node, base)  # noqa: E731
    elif tag == RDF + "RDF":
        channel = _child(root, RSS1 + "channel")
        doc = FeedDoc("rdf", _title(_child(channel, RSS1 + "title")) if channel is not None else "",
                      _link(base, _text_of(_child(channel, RSS1 + "link"))) if channel is not None else None)
        nodes = _children(root, RSS1 + "item")
        make = lambda node: _entry_from_rss(node, base)  # noqa: E731
    else:
        raise FeedError("This address does not contain a readable feed.")
    if len(nodes) > MAX_ENTRIES:
        doc.truncated = True
    for node in nodes[:MAX_ENTRIES]:
        entry = make(node)
        if entry.title or entry.summary or entry.content:
            doc.entries.append(entry)
    return doc


# ---- JSON Feed ---------------------------------------------------------------------------------

def _refuse_constant(_name):
    raise ValueError("not a number")


def _small_int(text):
    return int(text) if len(text) < 18 else 0


def _parse_json_feed(text: str) -> FeedDoc:
    try:
        data = json.loads(text, parse_constant=_refuse_constant, parse_int=_small_int, parse_float=lambda s: 0.0 if len(s) > 40 else float(s))
    except (ValueError, RecursionError, MemoryError):
        raise FeedError("This address does not contain a readable feed.") from None
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not version.startswith("https://jsonfeed.org/version/") or not isinstance(data.get("items"), list):
        raise FeedError("This address returned data, but not a feed the station can read.")
    doc = FeedDoc("jsonfeed", clean_text(data.get("title"), TITLE_CHARS), clean_url(data.get("home_page_url")))
    items = data["items"]
    if len(items) > MAX_ENTRIES:
        doc.truncated = True
    for item in items[:MAX_ENTRIES]:
        if not isinstance(item, dict):
            continue
        published, precision = _first_date(item.get("date_published"), item.get("date_modified"))
        basis = "published" if isinstance(item.get("date_published"), str) and parse_date(item["date_published"])[0] is not None else ("updated" if published is not None else "")
        text_content = item.get("content_text") if isinstance(item.get("content_text"), str) else ""
        html_content = item.get("content_html") if isinstance(item.get("content_html"), str) else ""
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        authors = item.get("authors") if isinstance(item.get("authors"), list) else []
        first_author = authors[0] if authors and isinstance(authors[0], dict) else {}
        entry = Entry(
            title=readable_text(item.get("title") if isinstance(item.get("title"), str) else "", TITLE_CHARS),
            link=clean_url(item.get("url")) or clean_url(item.get("external_url")),
            guid=clean_text(str(item.get("id", ""))[:300]) if isinstance(item.get("id"), (str, int)) else "",
            published_ts=published, date_precision=precision, date_basis=basis,
            summary=readable_text(item.get("summary") if isinstance(item.get("summary"), str) else "", SUMMARY_CHARS),
            content=clean_text(text_content, CONTENT_CHARS) if text_content else html_to_text(html_content, CONTENT_CHARS),
            categories=tuple(dict.fromkeys(clean_text(t, 60) for t in tags[:8] if isinstance(t, str) and clean_text(t))),
            author=clean_text(first_author.get("name"), 80),
        )
        if entry.title or entry.summary or entry.content:
            doc.entries.append(entry)
    return doc


# ---- public entry points -------------------------------------------------------------------------

def parse_feed(body: bytes, charset: str = "", base_url: str = "") -> FeedDoc:
    """Parse RSS, Atom or JSON Feed. ``base_url`` (the address the feed came from) resolves relative links.
    Raises ``FeedError`` with a plain-language message."""
    kind = sniff(body)
    if kind == "xml":
        return _parse_xml_feed(body, base_url)
    if kind == "json":
        return _parse_json_feed(decode_text(body, charset))
    raise FeedError("This address is a web page or plain text, not a feed.")


def read_page(body: bytes, charset: str, base_url: str) -> HtmlDoc:
    """Read a web page into visible text and metadata."""
    try:
        return parse_html(decode_text(body, charset), base_url=base_url)
    except HtmlLimit as exc:
        raise FeedError(str(exc)) from None
