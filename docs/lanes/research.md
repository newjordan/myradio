# Lane: research (interest-first researchers)

Package: `myradio/research/` (an Extension, loaded by `myradio.extensions.builtin_extensions()` as `research`).
Tests: `tests/test_research*.py`. This document is the contract the hosts lane, the director, the UI and the doctor build against.

## 1. Purpose and honest status

### What it is

A **researcher** is a user-owned, optional agent. It reads only the sources its owner chose (feeds or pages), keeps items that
match the owner's interests, are recent and have not been covered before, and leaves a **brief**: a short, sourced list of items
with verbatim extracts and timestamps. The on-air voice is somebody else's job: **this package never calls a language model**, never
writes a script, never touches audio. It fetches, parses, filters and extracts. Writing the spoken script is the hosts lane's job;
scheduling it on air is the director's.

Product rules it enforces (PRODUCT.md "Interest-first news and research", HANDOFF section 1):

- Positive and curious by default, optional, and silent until the operator creates a researcher. No default source list, no wire, no politics feed.
- Sources and exclusions are the operator's. Source names, links and publication/fetch timestamps are kept on every item.
- It never invents anything: extracts are verbatim substrings of the fetched text, every kept item carries the reasons it was kept, and the only
  sentences the package writes itself are plain status messages.
- If nothing suitable is fresh, the outcome is `nothing_new` (or `error`). A stale or already-covered item is never presented as new.
- Retrieved content is untrusted data. It is stored and returned as strings, never executed or followed, never placed in an operation result or in
  the status snapshot, and never reaches a speech job without the `*_speech` neutralisation described below.

### Status

| Real: run and observed in this session | Verified only against local fixtures or fakes | Unverified |
| --- | --- | --- |
| All 460 research tests pass on CPython 3.12.3, 3.11.15 and 3.13.14 (run again after the second fix round, section 10). | **No real web request was made.** Every "website" is a server on 127.0.0.1 (port 0) in the test process. | CPython 3.10 (the sources parse under the 3.10 grammar; nothing was run there). CPython 3.11 and 3.13 passed the 375 tests that existed before the review fixes; the 72 tests added since were not run there. |
| The fetcher's sockets, selector polling, TLS handshake and certificate checks run for real against local servers, including a TLS server with a throwaway certificate and SNI/Host observation. | "Public" addresses in the tests (93.184.216.x) are stand-ins: an injected connector dials the local server instead. The default connector is exercised only against loopback in fixture mode. | macOS (kqueue selector, system certificate store, `time.tzset`). |
| SQLite persistence, migrations, retention, the scheduler thread, run_now, review state machine, shutdown and thread cleanliness run for real in temp directories. | DNS: answers come from an injected resolver. The system resolver is exercised only for `localhost`. | Validation of certificates against the public CA store; any real site's feed, headers, compression or redirect habits; IPv6 connectivity. |
| The extension runs inside the real `ExtensionHost`, the real `Station` and the real HTTP `Server` (token, Host and Origin checks, route limits). | Feed content is hand-written fixtures plus 1,500 mutated copies per run, not captured real-world feeds. | A multi-day soak; behaviour behind proxies (proxies are unsupported by design) or captive portals; sites that block unknown agents; the page reader's hiding rules and its handling of end tags left out follow the browser rules as written in the HTML standard and were not compared with a real browser. |

Nothing else was run: no model, no GPU, no process outside this lane's tests, no network beyond loopback.

## 2. Public API

Everything below is importable from `myradio.research.<module>`. Only the standard library is used.

| Module | Role |
| --- | --- |
| `extension.py` | `build()`, `ResearchExtension`: operations, routes, status, checks, backup list. |
| `service.py` | `ResearchService`: scheduler thread, run pipeline, review queue, and the API the hosts lane and director use. |
| `fetcher.py`, `netguard.py`, `errors.py` | The safe HTTPS client, address rules and the plain-language error catalogue. |
| `feeds.py`, `htmltext.py`, `textutil.py` | RSS/Atom/JSON Feed parsers, the HTML text extractor, text and URL helpers. |
| `selection.py`, `model.py` | Scoring, exclusions, tone filter, de-duplication, the researcher model and cadence. |
| `store.py`, `schema.py` | SQLite persistence, and the Brief schema as code (`check_brief`). |
| `grounding.py` | `check_script(script, brief)`: an advisory lint that a spoken script stays inside the facts of a brief. |

### The service (what the director and hosts lane call)

`ResearchExtension.service` is the running `ResearchService` after `start()` (it is `None` before). All methods are thread-safe and bounded.
Expected failures raise `myradio.extensions.ExtensionError` with a plain-language message.

```python
service.next_airable(researcher_id=None, now=None) -> Brief | None   # oldest brief that may air now; None means "nothing suitable: play music"
service.claim_airable(researcher_id=None)          -> Brief | None   # next_airable + mark_aired as one atomic step (exactly one caller wins)
service.mark_aired(brief_id)                       -> bool           # True once; False if rejected, pending, stale, or already aired
service.release_brief(brief_id)                    -> bool           # undo mark_aired/claim when the broadcast never happened (e.g. speech job failed)
service.briefs(researcher=None, review=None, outcome=None, limit=20, brief_id=None, airable=False) -> list[Brief]   # newest first, limit 1..50; airable=True lists only briefs that may air now, and the limit applies to those
service.researchers()                              -> list[Researcher]
service.researcher(researcher_id)                  -> Researcher
service.sources()                                  -> list[SourceHealth]
service.save(researcher_dict) / remove(id) / set_paused(id, bool) / run_now(id, wait=0.0) / review(brief_id, "approved"|"rejected")
service.check_source(url)                          -> dict            # the "Check this address" action
service.status() -> dict ; service.checks() -> list[dict] ; service.tick() -> int ; service.start(run_thread=True) ; service.stop()
```

A `Brief` is always a plain JSON-safe `dict` (below). Epoch seconds (`*_ts`, float, UTC) are for arithmetic; ISO-8601 strings with an offset
(`*_at`) are for display and for API edges. They always describe the same instant.

How a consumer should use it:

1. **Director**: `brief = service.claim_airable(rid)` (or `next_airable` to peek, then `mark_aired` when the speech is queued). `None` means play
   music; do not pad. If the speech job fails for good, `service.release_brief(brief["id"])` makes it available again (review and freshness still apply).
   A brief may not be aired after `fresh_until_ts`; `next_airable` enforces it, a consumer holding an old copy must check `now <= fresh_until_ts`.
2. **Hosts lane**: hand the Brief over **as it is** (the dict from `claim_airable` or `next_airable`) to `hosts.compose_update` or `hosts.briefs.parse_brief`. The hosts lane owns the mapping
   (`myradio.hosts.brief_from_research`, `docs/lanes/hosts.md` section 4) and uses only the `*_speech` fields, which have every speaker or event marker and the characters `< > [ ] { } |` removed:
   `extract_speech` (or `headline_speech` when the source gave only a title) is the text, `source_name_speech` the source, `researcher_name` the topic, `created_at` the time it was made, and `url`, `published_at` and the item
   `id` travel for the citation. `kind` maps one to one, except that **`unverified` is read as `uncertain`**, so something the station cannot vouch for is voiced as "not confirmed", never as reporting. A Brief that is not `ready`
   has nothing to read, so music keeps playing. Never put `headline`, `extract`, `source_name` or `why` into a speech job yourself: they are retrieved text (see below). This lane does not ship a second mapping, so the two
   cannot disagree; `tests/test_research_schema.py` (`HandOverToHostsTests`) runs briefs that this lane really produces through the hosts lane's reader and fails if either side changes the shape.
   Two things to know. A source the operator did not name falls back to the feed's own title, and if the feed has none, to the site's address; the hosts lane does not read an item whose source is only an address, so **name each source**
   in the researcher's settings (tested). And a link longer than the hosts lane keeps (300 characters) costs the link, not the item (the Brief keeps it in full).
   Say nothing about when an item happened beyond `published_at` (and `date_precision`/`date_basis`) and add no facts that are not in the extract or headline. Keep to `target_words` for
   the whole segment. `anchor` is the persona id chosen on the researcher (may be `null`).
3. **Tests of consumers** may call `myradio.research.schema.check_brief(brief)`, which returns a list of problems (empty when valid). It enforces the forms written here: 16-hex item ids, an http(s) `source_url`, at most 5 categories, the source formats (`rss`, `atom`, `jsonfeed`, `rdf`, `page`, `text` or empty), skip counts keyed by the documented reasons only, and an anchor that is a persona slug.
4. **Grounding lint (advisory)**: `myradio.research.grounding.check_script(script, brief, allowed_words=(), item_ids=None, now=None) -> list[Finding]` (`Finding(code, text, message)`, `as_dict()`).
   It flags numbers, capitalised names, addresses and handles that no source mentions, an item whose source is not named, certainty words ("confirmed", "officially") about `unverified` or `commentary` items, freshness claims ("breaking", "just in") when nothing is under 30 minutes old,
   and a script more than 1.5 times `target_words`. Station names and persona names go in `allowed_words`; speaker and event markup is ignored; weekday and month names are calendar words. It compares words, not meaning, so it can miss an invented claim made of words the sources use and can flag a fair paraphrase:
   use it to refuse or regenerate a script, not as proof. Nothing in this lane calls it automatically.

### The Brief

Produced by every run, whatever its outcome. Fields (all always present):

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | 32 lowercase hex | The brief. |
| `researcher_id`, `researcher_name` | hex, text | Who made it (`researcher_name` as at the time of the run). |
| `outcome` | `ready` \| `nothing_new` \| `error` | `ready`: at least one item. `nothing_new`: at least one source was read (or unchanged) and nothing qualified. `error`: no source could be read. |
| `message` | text | One plain sentence written by the station (never contains text from the web). |
| `review` | `pending` \| `approved` \| `rejected` \| `auto` \| `null` | Only `ready` briefs are reviewed (`null` otherwise). `pending`: waits for the operator (researcher `review: "before_air"`, the default). `auto`: researcher `review: "auto"`, may air without a decision. |
| `trigger` | `scheduled` \| `manual` | `manual` is `run_now`. |
| `created_ts`, `created_at` | float, ISO | When the run finished. |
| `reviewed_ts/at`, `aired_ts/at` | float/ISO or `null` | Last review decision; when `mark_aired`/`claim` happened. |
| `airable` | bool | Computed when read: `ready` and `review` in (`approved`, `auto`) and not aired and `now <= fresh_until_ts`. `false` for a brief that has aired (until `release_brief`). |
| `freshness_hours`, `segment_seconds`, `target_words` | positive ints | The researcher's settings at run time; `target_words = segment_seconds * 2.5`. |
| `anchor` | persona slug or `null` | `[a-z0-9][a-z0-9-]{0,62}`. Not validated against the hosts lane's personas. |
| `fresh_until_ts/at` | float/ISO or `null` | `ready` only: the oldest item's `published_ts` plus `freshness_hours`. After this the brief can no longer air or be approved. |
| `items` | list of Item | Empty unless `ready`; 1 to 8 items, best first. |
| `sources` | list | One entry per configured source: `url`, `name`, `status` (`ok` \| `unchanged` \| `error` \| `skipped`), `message` (plain, ours), `entries` (posts read), `format` (`rss`, `atom`, `jsonfeed`, `rdf`, `page`, `text` or empty), `checked_at`. |
| `summary` | object | `considered` (posts seen), `kept`, `skipped` (counts by reason: `undated`, `future`, `too_old`, `already_covered`, `skipped_source`, `skipped_topic`, `not_relevant`, `tone`, `similar_to_aired`, `duplicate`, `source_limit`, `no_room`), `explanation` (one sentence, `ready` only). |
| `warnings` | list of text | When some sources failed but others worked: the plain-language reasons. |

An **Item**:

| Field | Type | Meaning |
| --- | --- | --- |
| `id`, `rank` | 16 lowercase hex, 1..n | Stable identity within this brief; rank 1 is best. |
| `headline`, `headline_speech` | text | Entry title (markup removed, whitespace collapsed; never empty: an untitled entry is introduced by the start of its text). `_speech` is neutralised. |
| `extract`, `extract_speech`, `extract_truncated` | text, text, bool | **A contiguous substring of the entry's cleaned text** (its summary, or its content or page text when the summary is under 40 characters), cut at a sentence end when possible, at most 200 to 700 characters depending on segment length. May be empty when the source gave only a title. |
| `source_name`, `source_name_speech`, `source_url` | text, text, address | The operator's name for the source, else the feed's title (retrieved text), else the host. `source_url` is the address that actually answered (the feed, after discovery or redirects). |
| `url` | address or `null` | The item's own page, tracking parameters removed. It is **never fetched** by this package; a screen must show it as text and open it only on a click (`rel="noopener noreferrer"`). |
| `published_ts/at`, `date_precision`, `date_basis` | float, ISO, `time` \| `day`, `published` \| `updated` \| `last_modified` | Always present for kept items (undated items are never kept). `day` means the source gave a date only and the start of that UTC day is used (so staleness is judged conservatively). `updated` means only an update time was available; `last_modified` means a page's HTTP date was used. |
| `fetched_ts/at` | float, ISO | When the source was read. |
| `kind`, `kind_reason` | `reported` \| `commentary` \| `announcement` \| `unverified`, text | Heuristic (below) and the reason, in plain words. |
| `score`, `matched`, `why`, `tone` | number, list, list, `{penalty, reasons}` | The relevance score after tone penalty, the interests or questions that matched, every reason the item was kept (matched words with where, age, source, tone penalties), and the tone detail. **`why` can hold retrieved text**: its "From a source you chose: <name>" line carries the feed's own title when the operator gave the source no name. Show it as text; never speak it. |
| `categories` | list of text (at most 5) | Tags the source supplied (retrieved text). |

`kind` rules, in order (heuristic, English cues, shown with a reason on every item): hedged wording ("reportedly", "rumor", "unconfirmed", "leaked", "sources say" ...)
gives `unverified` even from a source marked as news; an opinion/analysis/review tag, URL section or `Opinion:` style title prefix gives `commentary`;
otherwise the operator's label for the source (`reported`, `commentary`, `announcement`) is used; otherwise an announcement cue in the headline ("announces", "introducing",
"now available", "release notes" ...) gives `announcement`; otherwise `unverified`. **An unlabelled source is therefore `unverified` by default**: the station has no basis to
call it reporting, and says so. The package never verifies anything.

Lifecycle of a `ready` brief:

```
 run finishes ──► pending ──approve──► approved ──mark_aired/claim──► aired (final)
                    │  ▲                 │  ▲
                    │  └──approve────────┘  │            researcher review "auto": starts as auto (approve or reject still allowed)
                    └──reject──► rejected ◄─┘            approving after fresh_until_ts is refused; rejecting is always allowed
```

`nothing_new` and `error` briefs are history only: they cannot be reviewed, aired, or approved. Items placed in any `ready` brief (even one later rejected) are remembered as
covered and are not offered again, so a rejected brief is not re-made on the next run.

### Researcher object (what `research.save` takes and `GET /researchers` returns)

Input fields (an unknown field is refused with "The setting “x” is not recognised. Check the spelling, or leave it out."; the read-only fields below are ignored if echoed back, so a screen can send back what it received):

| Field | Required | Rules |
| --- | --- | --- |
| `id` | no | 32 hex; present to change an existing researcher. |
| `name` | yes | 1 to 80 characters. |
| `purpose` | no | up to 300 characters (what this is for; shown, not used for matching). |
| `interests` | yes | list (or text split on commas, semicolons and new lines) of 1 to 20 words or phrases, each up to 80 characters. |
| `questions` | no | up to 10, each up to 200 characters. Used as extra keywords (weight 0.8). **They are not answered**: there is no model here. |
| `sources` | yes | 1 to 20 items, each an address string or `{url, name?, kind?}` (`kind`: `reported`, `commentary`, `announcement`). Addresses are checked when saved (secure, port 443, not private or internal); a missing scheme gets `https://`. Give each source a `name`: without one the feed's own title is used, and a feed with no title falls back to the site's address, which the hosts lane will not read aloud as a source. |
| `exclude_topics` | no | up to 30 phrases to skip. |
| `exclude_sources` | no | up to 30: a site (`example.com` also covers its sub-domains), an address with a path prefix, or a source name. Choosing and skipping the same source is refused. |
| `cadence` | no (default every 24 hours) | `{"type": "every_hours", "hours": 1..168}` or `{"type": "daily", "time": "HH:MM", "timezone": "<IANA name>", "weekdays": "1,2,3,4,5"}` (ISO days, Monday=1; default every day; a list of ints is accepted). |
| `freshness_hours` | no (24) | 1 to 720. Items older than this are never used. |
| `segment_seconds` | no (60) | 15 to 600. Sets the number of items (about one per 20 seconds, 1 to 8) and the extract length. |
| `anchor` | no | persona slug or empty. |
| `review` | no (`before_air`) | `before_air` (the operator approves each brief) or `auto` (a brief needs no approval, so it can be used on air; this lane never puts anything on air). |
| `tone_filter`, `tone_words`, `tone_allow` | no (`true`, none, none) | The tone filter switch, extra phrases to down-rank (weight 1.5), and built-in phrases never to down-rank. |
| `paused` | no | New researchers start active unless `true` (a "draft"). Existing ones keep their state unless this is given. |

Read-only output fields: `state` (`idle` \| `queued` \| `running` \| `paused`), `cadence_text` ("Every 6 hours", "Weekdays at 08:00 (Europe/London)"), `created_*`, `updated_*`, `next_run_ts/at`,
`last_run` (`{ts, at, outcome, message, brief_id}` or `null`), `pending_review`, `consecutive_errors`. `research.save` also returns `notes`: plain suggestions (for example when "how recent" is shorter than the run interval, or when a source address has a value in it that looks like a password or key; see section 3).

### Lower-level API

- `fetcher.Fetcher(...)`: `fetch(url, validators=None, deadline_seconds=None) -> FetchResult` (url, final_url, status, not_modified, content_type, charset, body, etag, last_modified,
  server_date, redirects, requests, elapsed); `check_url(url) -> str` (offline validation); `cancel` (a `threading.Event`). Raises `errors.FetchError(kind, message, status, retry_after, host)`.
  Kinds: `bad_url`, `not_https`, `bad_port`, `blocked_name`, `blocked_address`, `dns`, `dns_timeout`, `dns_busy`, `connect`, `timeout`, `certificate`, `tls`, `too_large`, `bad_type`,
  `bad_response`, `http_error`, `rate_limited`, `too_many_redirects`, `redirect_loop`, `daily_cap`, `cancelled`, `backoff`, `unsupported_encoding`. `message` is always a plain sentence.
  `loopback_fixture`, `connector`, `resolver` and `ssl_context` are **constructor arguments for tests only**; nothing reads them from configuration or operations.
- `feeds.parse_feed(body, charset="", base_url="") -> FeedDoc(format, title, link, entries, truncated)` (`base_url` resolves relative links), `Entry`, `feeds.read_page(...)`, `htmltext.parse_html(html, base_url, fragment, seconds)`,
  `htmltext.html_to_text(fragment, limit)`, `htmltext.readable_text(value, limit)` (HTML when it holds real HTML, otherwise plain text) and `htmltext.looks_like_html(text)`. Errors: `FeedError` (plain message), `HtmlLimit`.
- `selection.select_items(spec, candidates, seen, now) -> Selection(items, skipped, considered, used, fresh_until)`; pure and deterministic (independent of candidate order).
- `model.normalize_researcher(raw, validate_url) -> spec` (raises `ExtensionError`), `model.nominal_next(cadence, now, last_start)`.
- `schema.check_brief(brief) -> list[str]`; `schema.BRIEF_KEYS`, `ITEM_KEYS`, `KINDS`, `REVIEWS`, `OUTCOMES`, `SKIP_REASONS`, `SOURCE_FORMATS`.
- `textutil.neutralize_speech(text)`, `clean_text`, `clean_url`, `canonical_key`, `parse_date`, `build_extract`.

### How an item is chosen (heuristic, and says so)

Counting words, not understanding stories. For each interest (and each research question) against the headline, the entry's tags and the first 3,000 characters of its text:
the whole phrase in the headline scores 6.0, in the tags 5.0, in the text 3.5; otherwise by words (filler words dropped, capitals, accents and simple plurals ignored: a word and its plural share one stem however each is written, so `indie` and `indies`, `hero` and `heroes`, `house` and `houses`, `analysis` and `analyses`, `quiz` and `quizzes` match each other; irregular plurals such as `mice` or `knives` are not folded): a one-word interest
scores 3.0 in the headline or 1.5 in the text, a longer one needs 60% of its words and scores `1 + 2*share + share in headline`. Relevance is the best term plus a quarter of the next three
(maximum 10). An item is kept only if relevance minus the tone penalty is at least 1.5. Skipped topics are matched broadly (a multi-word topic also counts when all its words appear
near the top). Freshness is elapsed time against `freshness_hours` (items dated more than 15 minutes ahead, or with no usable date, are never used). Duplicates are removed by canonical
address (scheme, `www.`, default port, tracking parameters, parameter order, fragment and trailing slash ignored), by headline similarity (70% shared words) and by identical opening text;
anything similar to something already covered is held back. No source supplies more than two items while another source that is still under that limit has something to offer, and each extra item from one source counts for three quarters of the one before it. When everything that is left comes from sources already at the limit, the limit is lifted so that a long segment is not left short (a segment with room for 8 items, one source with 8 good items and another with 1, gets 8 items, not 3). **Tone filter**: a visible built-in word list of outrage,
fear and click-bait wording (`GET /tone` returns it) lowers an item's score by the word's weight (full in the headline, half in the opening text, at most 6.0); it never raises a score and
nothing is boosted for sounding cheerful. It is a word list, not sentiment analysis, and it can both miss bait and mark a fair story down.

**Reading feeds.** XML is parsed strictly with `xml.etree`; any document type declaration (and so any entity definition) is refused, nothing is ever expanded or fetched. The refusal is made by looking at the start of the document **before the XML parser is given a single byte**: a declaration can only stand ahead of the root element, so the part ahead of it (comments and processing instructions are skipped, UTF-16 and UTF-32 text is looked at too) is checked, and the parser's own refusal remains as a second layer. This does not depend on how a particular copy of the parser limits entity expansion. Text elsewhere in the feed that merely contains the words (a CDATA section quoting an HTML page) is not a declaration. The message is `This feed starts with a kind of header that the station does not accept for safety reasons, so it was not read. Look for the site's plain RSS or Atom feed address instead.` RSS 2.0 declared in a default namespace (`xmlns="http://backend.userland.com/rss2"`) is read like any other RSS. Only if a strict parse fails, the text is retried once after repairing the common slips of real feeds: blank space before the XML declaration, HTML entity names XML does not define (`&nbsp;`, `&copy;` become numeric character references), a bare `&`, and control characters (UTF-16 text is not repaired). An entity name nobody defined is still an error. Relative item links are resolved against the address the feed came from. Text from every field passes through one cleaner that removes control characters, invisible and direction-changing characters, the default-ignorable characters that draw nothing (soft hyphen, combining grapheme joiner, the Hangul and Khmer fillers, the Mongolian variation selectors, the half-width Hangul filler), the whole Unicode "tag" block U+E0000 to U+E0FFF (the tag characters and the variation-selector supplement, both used to smuggle hidden text), private-use and lone surrogate characters, and collapses whitespace. The emoji variation selectors (U+FE00 to U+FE0F) and the joiners U+200C and U+200D are kept because emoji and some scripts need them; `neutralize_speech` applies the same removal, so it is safe on text that was not cleaned first.

**Plain text and HTML in fields.** A title, summary or content field that holds real HTML (a known element name, a comment or declaration, or a character reference) is read as HTML: hidden parts are dropped and entities are decoded. Any other text is plain text and keeps its angle brackets, so `Using List<String> in Java` or `Contact <info@club.example>` come through as written. Atom `type="html"` and `"xhtml"` are always HTML, and JSON Feed `content_text` is always plain text. Text that is plain by this test but contains an unknown element with a `hidden` attribute is shown literally (visible to the person who approves briefs) rather than silently dropped.

**Pages.** The reader drops script, style, comments and anything hidden by the usual means, and treats the elements whose end tag may be left out (`p`, `li`, `dt`, `dd`, `tr`, `td`, `th`, the table sections, `option`, `optgroup`) as a browser does: a new sibling ends the previous one, and a block element ends an open paragraph even through inline markup. A page with hundreds of unclosed paragraphs or list items is therefore read, not refused as "nested too deeply"; real nesting of 400 levels still is. A badly written canonical or feed address in a page costs that one link and never the page.

### Safe fetching (summary of what the tests prove)

https only on port 443 (user names, odd IPv4 spellings such as `127.1` or `2130706433`, single-label and internal-looking names are refused before any lookup); every hop (the first request
and up to 3 redirects, each re-parsed) resolves the name once and refuses the whole name if any answer is loopback, private, link-local, multicast, reserved, a cloud metadata address (169.254.169.254,
169.254.170.2, 100.100.100.200, 168.63.129.16, 192.0.0.192, fd00:ec2::254), IPv4 wrapped in IPv6 (judged by the IPv4 address), NAT64, 6to4 or Teredo; IPv6 is allow-listed to 2000::/3 minus special
ranges. The connection goes to a checked address with the original name used for SNI, certificate verification and `Host`, so a second DNS answer cannot redirect it. One absolute deadline
(default 20 seconds) covers lookup, connect, handshake, headers, body and all redirects; sockets are polled in 0.2 second slices, so a drip-feeding server or a shutdown request never holds a
thread. Responses are capped at 2 MiB on the wire and 2 MiB after decompression (gzip, deflate), headers at 64 KiB and 100 lines; only RSS, Atom, JSON Feed, HTML and plain-text content types are accepted. No cookies, no `Authorization`, no
`Referer`, no proxies, no connection reuse. Conditional requests (`If-None-Match`, `If-Modified-Since`) are sent only to the exact address they were recorded for, with values re-validated.
Per-host spacing (3 seconds by default) honours `Retry-After`. The wait (never more than 30 seconds per request; longer waits are refused) is not part of the request deadline: the deadline moves on by the time spent waiting, so a 25 second gap with a 20 second deadline is waited out and the request then gets its full 20 seconds. A redirect whose `Location` cannot be read as an address (for example `//[`) is an ordinary "not a valid web address" failure for that source. A daily request cap (500 by default) is kept in the database.

## 3. Persistence

One private SQLite file, `<data>/ext/research/research.sqlite3` (mode 0600, WAL, `synchronous=NORMAL`, foreign keys on), opened with `ctx.open_db`. Nothing is stored in the station database.
Migrations: `PRAGMA user_version` via `myradio.extensions.migrate`; `store.MIGRATIONS` currently has one step. Data written by a newer version makes the extension fail visibly
("This data was written by a newer myradio. Update before opening it.") and is left untouched.

| Table | Holds |
| --- | --- |
| `researchers` | id, name, `spec` (the validated settings as JSON), paused, version (bumped on every edit), schedule (`next_run_ts`), last run summary, consecutive errors. |
| `briefs` | One row per run: id, researcher, outcome, review state, review/aired times, `fresh_until_ts`, `body` (JSON: items, sources, summary, warnings). |
| `seen` | Items already used (key, title signature, time), per researcher. |
| `source_state` | Per researcher and address: final address, discovered feed address, ETag, Last-Modified, last status and message, post count. |
| `usage` | Requests per UTC day (the daily cap survives restarts). |

Retention, applied in the same transaction as every committed run: newest 30 `ready` briefs and 10 other briefs per researcher; `seen` for 60 days and at most 2,000 items per researcher;
`usage` for 30 days. "Newest" means last stored, not latest by the clock: a computer whose clock is set back can no longer make a run delete the brief it has just made (and listings, `next_airable` and the memory of covered items follow the same order). Removing a researcher cascades to everything above. A run whose researcher was removed, or edited while it ran, is discarded (an edited researcher runs again at once with the new settings).

**Backups.** `persistent_files()` returns `research.sqlite3` and `config.json` (operator-written, optional). The fetcher sends no credential of its own and stores none, **but a source address is kept exactly as typed**, and some private feed
addresses carry a key in them (`https://example.org/private.atom?token=...`). Such an address is in `research.sqlite3` (so in a backup) and is returned by `research.save`, `GET /researchers`, `GET /sources` and a brief's `sources[].url` and
`items[].source_url`. Error messages, status and checks name only the host, never the rest of the address. When a source's query has a value under a name that looks like a password or key (`token`, `key`, `api_key`, `secret`, `auth`, `sig`, `password` and
similar), `research.save` adds a plain note saying so and naming only the host, never repeating the value, so the person can decide. Nothing is masked: the station needs the full address to fetch it, and a screen needs it to edit.
The files are personal (interests, chosen sources, possibly such an address), so keep them out of public archives. The database is in WAL mode: copy it only while the service is stopped (the product's rule for backups anyway);
`stop()` checkpoints and leaves a single file (tested). No file holds a secret by design, but see the note on addresses above. `config.json` keys (all optional, numbers are clamped, unknown keys are ignored and reported by the doctor check):
`daily_request_cap` (10 to 5000, default 500), `host_min_interval_seconds` (1 to 30, default 3; 30 is the longest wait the fetcher will sit through, so a larger setting could only make a second source on the same website fail), `request_timeout_seconds` (5 to 60, default 20), `contact` (an address or email added to the user agent).
Nothing in `config.json` or in any operation can enable the loopback fixture mode, plain http, a proxy, other ports or a different size cap (tested).

## 4. Extension surface

### Operations (typed by the host; names are `research.<name>`)

| Operation | Arguments | Result | Notes |
| --- | --- | --- | --- |
| `save` | `researcher` (object) | `{researcher, created, notes}` | Creates or, with `id`, updates. A cadence change reschedules; any edit forgets cache validators. At most 50 researchers. |
| `remove` | `researcher_id` (string, 32) | `{removed}` | Deletes the researcher, its briefs and its memory. |
| `pause` | `researcher_id` | `{researcher_id, paused, state}` | Stops scheduled runs. Settings and briefs stay. A paused researcher can still be run by hand. |
| `resume` | `researcher_id` | `{researcher_id, paused, state, next_run_at}` | Schedules it again. |
| `run_now` | `researcher_id` | `{researcher_id, state, message}` plus, when it finished within 8 seconds, `outcome`, `brief_id`, `items` | `state`: `done`, `running`, `queued` or `stopped`. Always looks at the sources again (no conditional requests). The scheduler thread does the work; the call only waits. |
| `review` | `brief_id`, `decision` (`approved` \| `rejected`) | `{brief_id, researcher_id, review, changed}` | See the lifecycle above. |

No operation result, and nothing in the status snapshot, contains text taken from the web (only ids, counts, outcome words and sentences the station wrote). Even failure messages name only the address the operator chose: when a website redirects the
station, or a page points at a feed, a failure at that other address is reported as "<your address> sent the station on to another address. <reason>" and never repeats the address the website picked (tested with hostile host names). The arguments carry no command, endpoint,
credential, header or size option, so a chat model or external agent cannot make the station run anything or change how it fetches. A saved *source address* is data the station will request
(under the guard above); anything that may call `research.save` can therefore cause requests to public addresses it chooses (never private ones), bounded by the per-host gap and the daily cap.
Chat-model calls already need single-use approval; MCP calls are immediate by design (HANDOFF section 8).

### Routes (all under `/api/x/research`; the host adds token, Host and Origin checks; JSON in and out; errors are `{"error": "<plain sentence>"}` with status 400)

| Route | Request | Response |
| --- | --- | --- |
| `GET /researchers` | none | `{researchers: [Researcher], limits: {...}}` (`limits` lists every bound in section 6 that a form needs) |
| `GET /briefs` | query: `researcher` (id), `review`, `outcome`, `limit` (1..50, default 20), `id`, `airable=1` | `{briefs: [Brief]}`, newest first. `airable=1` keeps only briefs that may air now, **decided before the limit is applied**, so a ready update is found even when many newer "nothing new" runs are in front of it (it always agrees with `next_airable`). |
| `GET /sources` | none | `{sources: [{url, host, name, kind, used_by: [{id, name}], status, message, checked_ts, checked_at, format, entries, feed_url}]}`; `status` is `ok`, `unchanged`, `error` or `unchecked` |
| `GET /tone` | none | `{explanation, words: [{phrase, weight, list}], penalty_cap, minimum_relevance}` |
| `POST /sources/check` | `{"url": "..."}` (max 4 KB) | `{ok, address, kind: "feed"\|"page", format, title, posts, dated, feed_url, sample: [up to 3 headlines], message}`; nothing is saved. A failure is a 400 with the plain reason. Refused with "busy" while a researcher is running. |

The responses of `GET /briefs`, `GET /sources` and `POST /sources/check` contain retrieved text (titles, extracts, feed names). A screen must render it as inert text, never as markup, and never
follow an item link automatically.

### Status (`station.status()["extensions"]["research"]["status"]`)

Cached, lock-free and O(1); replaced as a whole whenever something changes (tested with the database and every lock made unavailable).

```json
{"enabled": true,
 "scheduler": {"running": true, "last_tick_at": "2025-11-05T00:00:00+00:00", "problems": 0},
 "researchers": [{"id": "...", "name": "Backyard astronomy", "paused": false, "state": "idle",
                  "next_run_at": "2025-11-05T06:00:35+00:00",
                  "last_run": {"at": "...", "outcome": "ready", "message": "2 updates ready. Waiting for your review.", "brief_id": "..."},
                  "pending_review": 1}],
 "totals": {"researchers": 1, "pending_review": 1, "requests_today": 12, "daily_cap": 500}}
```

`last_run` is `null` until the first run. `problems` counts scheduler hiccups (a crash inside one pass; the thread carries on). In the status a researcher's name is cut to 40 characters (the full name is on `GET /researchers`) so that fifty researchers always fit the host's 64 KB limit: with the longest possible names (80 emoji take 12 bytes each in JSON) and 400-character messages, a test measures 57,045 bytes for fifty.

### Doctor checks (`station.extension_checks()`, ids prefixed `research.`)

| Id | Meaning |
| --- | --- |
| `network` | `unknown` until a source has been tried; `ok` after a success ("A website was reached 5 minutes ago."); `warn` after a failure to reach anything; `fail` after three in a row. Only DNS, connect and timeout failures count, **and only when the run they happened in reached no website at all**: a mistyped or retired address among sources that answered says something about that address, not about the internet, whatever order the sources are listed in. A site answering with an error counts as reached. An address typed into "Check this address" never counts either way against the verdict. |
| `clock` | `fail` if the clock is before 2024; `warn` if it moved backwards since the last run, or differs by more than 5 minutes from the median of recent websites' `Date` headers. |
| `allowance` | `ok` below 80% of the daily request cap, `warn` from 80%, `fail` at the cap. |
| `certificates` | `warn` if the computer has no list of trusted website certificates (hint for macOS: "Install Certificates.command"). Absent when a test fetcher is injected. |
| `settings` | Only present when `config.json` could not be read or had unused or out-of-range keys. |

### Events consumed

None. The extension does not define `on_event`, so the host starts no mailbox thread for it. Researchers run on their own schedule whether or not the station is playing.

### Scheduler

One daemon thread, `myradio-research`. It wakes about every 30 seconds (each wait is 30 seconds plus or minus 10%), runs at most one researcher at a time (at most 10 per wake), manual requests first, then due researchers
by due time. Choosing and running happen under one lock, so a researcher is never picked twice. Each run has a 180 second budget (remaining sources are reported as skipped). New and rescheduled runs get a
deterministic per-researcher jitter of up to 60 seconds. After a failed run (`error` outcome) the next attempt is 20 minutes later, twice, then back on the normal schedule. Overdue researchers (the station was off) run once at start-up. A stored due time that cannot come from the
schedule (the clock was set back) is repaired at start. Daily schedules use the station's recurrence rules (`myradio.scheduling.next_occurrence`): local wall time in an IANA zone, a time that does not exist on a spring-forward day is skipped, a repeated time on a fall-back day runs once.
Other threads: at most 2 short-lived name-lookup helpers (`myradio-research-dns`), because a system lookup cannot be cancelled. `stop()` cancels the in-flight request, joins the thread, checkpoints and closes the database (tested: no thread and one file remain).

## 5. UI contract (for the Discover screen, the Operator and the phone view)

The screen is built by the UI lane in `myradio/web/panels/`; this lane ships no web assets. Everything the screen needs is in the routes above, and everything it can do goes through the operations (so chat and MCP can do the same).

**Researcher list** (the preview's "Your researchers"): for each researcher show the name; a status word from `state` and `paused` (`ON` for `idle`/`queued`/`running`, `PAUSED`, and `DRAFT` for a paused researcher whose `last_run` is `null`);
`cadence_text`; "read by <anchor>"; chips for `interests` and "skips <topic>" chips for `exclude_topics`; a "Review before airing" switch (`review`); **Run now**; pause/resume; edit; remove (confirm in the page, not with a browser box).
When a run is in progress show "Checking now..." (`state` = `running`/`queued`); poll `GET /researchers` or the status every few seconds while it is.
Show `pending_review` as a badge ("2 updates waiting for you").

**Latest result**, from `last_run` and the newest brief:
- `ready`: "Latest update, <time>": the items (headline, source name, time, "Why these" = `summary.explanation`, and each item's `why` on demand), a small label from `kind` ("News", "Opinion", "Announcement", "Unconfirmed" with `kind_reason` on demand),
  Approve and Reject buttons while `review` is `pending` ("Waiting for your review." / "It does not need your review, so it can be used on air." comes from `message`; nothing here promises that it will air, which is the director's decision). Show `warnings` quietly.
- `nothing_new`: "Nothing new and interesting this time. Nothing was made up to fill the gap." plus the counts from `message`. Never show the previous brief as if it were new.
- `error`: the `message`, with "Try again" (Run now) and, if a source failed, a link to that source's row.

**Empty states** (always with a next action): no researchers: "You have no researchers yet. A researcher watches websites you choose and brings back short, sourced updates about what you care about. Add one to start." (button **Add a researcher**);
no sources on a researcher: "Add the websites or feeds this researcher should read."; no briefs yet: "Nothing has been checked yet. Press Run now to try it."; paused: "Paused. Resume to start checking again."

**Add and edit form**: fields in plain words: "What should it be called?", "What would you like to hear about?" (interests, tag input), "Anything you would like answered?" (questions, optional), "Which websites or feeds should it read?" with an **Add** and a **Check this address** action
(`POST /sources/check` shows "Found a feed called ... with 12 posts (12 with dates)" or the plain error), a per-source "What kind of site is this?" (News or reporting / Opinion / Announcements from the maker / Not sure = unlabelled, which airs as "unconfirmed"),
"Topics or sites to skip", "How often?" (every N hours, or every day / weekdays at a time **in the station's time zone, which the screen must send**), "How recent must items be?" (hours; default 24), "How long should the update be?" (seconds, shown as about N seconds),
"Who reads it?" (host chooser, optional), "Review each update before it airs" (on by default; turning it off shows a one-line explanation that updates then need no approval and can be used on air without your review). Show the `notes` from the save result. A tone section: "Skip outrage and fear bait" switch, "Always allow these words", "Also avoid these words", and a link that shows `GET /tone`
("This is a plain word list, not an understanding of the story.").
The chat sentence "Give me a short, upbeat daily update on independent electronic music, backyard astronomy, and people building useful things. Skip national politics." is turned into a `research.save` call by the connected AI operator
(this package never interprets sentences); the screen shows it as a draft (`paused: true`) the person checks before turning it on.

**Sources view** (`GET /sources`): name, address, which researchers use it, last check time and `message`; `error` rows get a short "what to do" from the message itself.

**Errors**: every error string from the routes and operations is a complete sentence in plain language and can be shown as is. They never contain addresses of internal systems, stack traces or text from the remote site.
Examples: "Only secure web addresses can be used. Use the same address starting with https://."; "The setting “mood” is not recognised. Check the spelling, or leave it out."; "Add at least one item to Interests."; "That address sent the station around too many times (more than 3 redirects), so it stopped.";
"The station has used its allowance of 500 web requests for today. Researchers will carry on tomorrow."; "These updates are now older than you asked for, so they can no longer air as new. Run the researcher again to get fresh ones.";
"The station is busy checking your researchers. Try again in a minute." The full catalogue is `errors.describe(kind, host)`.

**Never** present a brief, item or source name as instructions, never render retrieved text as HTML, never auto-open item links, and never show a `nothing_new` or `error` run as news.

## 6. Limits and bounds

| Area | Bound |
| --- | --- |
| Researchers | 50; sources 20; interests 20 (80 characters); questions 10 (200); skipped topics 30 (80); skipped sources 30 (200); tone words 50 and allowed words 50 (60); name 80; purpose 300; source name 80. `research.save` input is at most 64 KB (host rule). |
| Numbers | `freshness_hours` 1 to 720 (24); `segment_seconds` 15 to 600 (60); every `hours` 1 to 168. Items per brief 1 to 8 (about one per 20 seconds of segment), extracts 200 to 700 characters, 2 per source. |
| Fetch | 20 s absolute deadline per source (all hops; config 5 to 60), not counting the polite waits below; 3 redirects; 2 MiB wire and 2 MiB decoded; 64 KiB and 100 lines of headers; 4 addresses tried, 6 s each; 3 s between requests to one host (config 1 to 30), each wait at most 30 s and waits over 30 s refused instead of slept; `Retry-After` capped at 1 hour; 500 requests per UTC day (config 10 to 5000). |
| Parsing | XML 60,000 elements, depth 64, 3,000,000 characters; 300 entries read per feed; fields: title 300, summary 3,000, content 6,000, page text 6,000 characters; HTML: 600,000 characters in, depth 400 (elements whose end tag may be left out do not pile up), 400 blocks, and 2.5 s checked between 16 KB slices of input (so it bounds the whole read; every step inside a slice is linear in the slice, and the style-attribute comment scan that once took minutes is now a plain scan); JSON numbers over 17 digits and non-finite values are neutralised or refused; 300 candidates scored per run. |
| Interpreter lock | This package runs inside the station process and shares the interpreter lock with the playout clock, which gives up catching up after 0.5 s. No single call on text a feed controls holds the lock for long: the HTML-or-plain-text gate searches 16 KB windows, feed repair works in 64 KB pieces cut where no reference can straddle, blanks ahead of the feed are skipped 64 KB at a time, UTF-16 and UTF-32 are decoded 64 KB at a time, and long text is cleaned 64 KB at a time (each gives exactly the answer one pass would; tests check it at the slice edges). Measured with a thread ticking every 5 ms while 2 MiB hostile inputs were read: its longest wait was 10 to 21 ms on every path through `parse_feed` (it was 145 to 560 ms before; table in section 10). `neutralize_speech` is still one pass (about 350 ms on 2 MB of `<|`), but it is only ever given a headline (300 characters), an extract (at most 700) or a source name (80): 0.17 ms at 700. `fold` on 2 MB held the lock about 33 ms. |
| Run | 180 s per researcher run; 10 runs per scheduler wake; 1 scheduler thread plus at most 2 lookup helpers; 8 s maximum wait inside `run_now`. |
| Storage | 30 ready and 10 other briefs per researcher; 60 days or 2,000 memory entries; 30 days of request counters; status under 60 KB for 50 researchers; response of any route bounded by these numbers. |

## 7. Tests

Files (`tests/test_research*.py`; 460 tests in all; the support module has none):

| File | Tests | Covers |
| --- | --- | --- |
| `test_research_netguard.py` | 23 | URL rules, odd IPv4 spellings, every address class (incl. mapped, NAT64, 6to4, Teredo, metadata), the IPv6 allow-list standing alone with the standard library's own flags silenced, whole-name refusal on a mix, address fuzz, the bounded resolver (stuck lookup, thread cap, cancel). |
| `test_research_fetcher.py` | 72 | Real TLS and plain fixture servers: SNI/Host/certificate checks, header hygiene, no cookies, blocked addresses (22 forms, as resolved names and as literals), a public IPv4 and IPv6 literal reaching the certificate check, DNS that changes after the check, redirects (private targets, downgrade, port, loops, 3 allowed / 4th refused, a `Location` that is not an address), absolute deadline, slow-loris headers and body, silent server, stuck DNS, cancel, size caps, compression bombs, malformed replies, a first address that cannot be reached and at most four tried, conditional requests, politeness (fake clock; a wait longer than the deadline is not a timeout), daily cap. |
| `test_research_feeds.py` | 105 | No call on feed-controlled text holds the interpreter lock long enough to stall the playout clock (13 tests: a 5 ms heartbeat must not wait 0.1 s, and every sliced call matches one pass at its slice edges). RSS/Atom/JSON Feed/RDF good, odd and hostile (including RSS in a default namespace and text with angle brackets that is not HTML); XML bombs (billion laughs, quadratic blow-up, external entities and DTDs never read or fetched, UTF-16 and UTF-32 variants; a declaration refused before the parser is given a byte, and text that only mentions one accepted), deep/wide documents, JSON hazards, common real-world slips repaired (blank lines before the declaration, HTML entity names such as `&nbsp;`, bare `&`, control characters) without ever accepting a DOCTYPE, relative links, the HTML extractor (36 hiding techniques, unclosed and self-closed hidden elements, limits, a style full of unclosed comments read in milliseconds while another thread keeps running, end tags left out, bad canonical addresses), invisible characters (including the variation-selector supplement), speech neutralisation, dates (also independent of the machine's time zone and language setting), extracts, 1,500 mutated documents per run. |
| `test_research_selection.py` | 63 | Scoring, plural folding (75 word pairs share one stem), exclusions, tone filter with reasons (a word counted once), kinds, duplicates and the 70 per cent threshold, covered-before memory, front-page links, diversity (the limit of two, the discount, lifting the limit when the others run out), freshness (including daylight saving), item shape and ids, verbatim extract property, prompt-injection text staying inert, determinism. |
| `test_research_service.py` | 72 | The pipeline against local servers: `ready`, `nothing_new`, `error`, partial failure, odd sources (a redirect that is not an address, a bad canonical address, an unexpected failure in one source) costing only that source, hostile content and website-chosen addresses not reaching status or results, review state machine, oldest-first airing, atomic claim by 8 threads, release, persistence and restart, migrations, retention (what survives, not just how many; a clock set back), daily allowance and its housekeeping, conditional requests, page sources, feed discovery and a feed that moves, mid-run removal and edit, crash recovery, the 50-researcher limit. |
| `test_research_scheduler.py` | 41 | Cadence and daylight saving (spring-forward gap, fall-back repeat), jitter, schedule repair, retries, run_now, pause/resume, the 10-per-wake bound, one job at a time, the real thread, shutdown during an in-flight request, status shape, lock-freedom and size with the longest names, doctor checks (dead addresses among working ones, a run that reaches nothing, the check box). |
| `test_research_extension.py` | 42 | Operation catalogue and argument validation, plain-language validation messages (including the password-in-an-address note), config that cannot relax a safety rule, the pause setting bounded by what the fetcher will wait, the station's own `build()` keeping the daily count across a restart, routes (`airable` decided before the limit), and the real `Station` and HTTP server (token, Host, Origin). |
| `test_research_schema.py` | 19 | Every brief the service makes passes `check_brief`; bad briefs are rejected (ids, links, categories, formats, skip reasons, anchors); briefs the pipeline really makes are read by the hosts lane's own `parse_brief` (kinds, speech-safe text, topic, citation, hostile wording, long links, named sources, nothing to read). |
| `test_research_grounding.py` | 23 | The grounding lint: grounded scripts pass; invented numbers, names, links, missing attribution, certainty and freshness overclaims, over-long scripts; markup ignored; very long or capital-heavy scripts checked in a moment; a real brief's honest script passes and an embellished one does not. |

Commands (each under `nice -n 19 ionice -c3`):

```
cd /home/frosty40/myradio && nice -n 19 ionice -c3 timeout 900 python3 -m unittest discover -s tests -p "test_research*.py" -v
cd /home/frosty40/myradio && nice -n 19 ionice -c3 timeout 900 python3 -m unittest discover -s tests -p "test_extensions*.py"
cd /home/frosty40/myradio && nice -n 19 ionice -c3 timeout 900 python3 -m unittest discover -s tests -p "test_*.py"
```

To run the same set on another Python (3.10 to 3.13), use that interpreter in place of `python3` in the first command, from the repository folder. The builder ran it on a CPython 3.11 and a CPython 3.13 interpreter (with `-X dev` on 3.13) before the review fixes; the fixer that followed ran only the default `python3` (3.12.3) and used no interpreter outside the repository.

Measured results are at the end of section 9. Test servers bind 127.0.0.1 port 0 only, and every test removes its threads, sockets and temp directories.

## 8. Requested changes to shared files

None of these was made here.

1. **`myradio/extensions.py` (orchestrator).** The director needs the research service object, but `StationFacade` deliberately exposes only `status`, `import_audio`, `action` and `extension_checks`. Please add a small read-only accessor,
   for example `ExtensionHost.get(name) -> Extension | None` and `StationFacade.extension(name)`, so the director can call `ctx.station.extension("research").service.claim_airable(...)`. (Operations were rejected as the hand-over path on purpose: they are visible to chat and MCP,
   and briefs contain untrusted text.)
2. **`myradio/web/**` (UI).** Add the Discover panel described in section 5. The preview's Discover view is the intended shape.
3. **`myradio/backup.py` / backup integration (orchestrator).** Include `ExtensionHost.backup_files()["research"]` (`research.sqlite3`, `config.json`) in portable backups, taken with the service stopped. Neither file holds a credential of the station's, but the database holds each source address exactly as the person typed it, and some private feed addresses carry a key in them (section 3). A backup should be treated as private.
4. **`README.md`, `PRODUCT.md`, `docs/HANDOFF.md`, `docs/RELEASE-STATUS.md`.** They still say research agents are not implemented. Suggested wording: "Interest-first researchers exist as an extension: verified against local fixtures only; no real web request has been made in testing; briefs are consumed by the hosts lane and the director".
5. **Nothing needed in `pyproject.toml`**: `myradio.research` is picked up by `packages.find include = ["myradio*"]` and ships no data files or entry points.
6. **Hosts lane / director.** Hand the Brief over as it is (section 2); the hosts lane maps it. `purpose` and `interests` are operator-written configuration, not retrieved text, and are not neutralised: neutralise them before speaking them (the hosts lane neutralises the topic it is given). **Doctor lane**: `research.*` check ids are in section 4.
   The hosts lane has already made the one change this lane would have asked for: a missing or unrecognised `kind` is now read as `uncertain` rather than `reported`.
7. **Operator (chat) wording**, optional: the six operations appear in the catalogue automatically; descriptions are under 400 characters.

## 9. Known limitations and unverified claims

- **No real web request was made.** Behaviour against real sites, real DNS, the public certificate store, IPv6, proxies, CDNs, bot blocking and unusual feeds is unverified. CPython 3.13 turns on strict certificate checking in `ssl.create_default_context()` (per its release notes), which could refuse a few sites with old-style certificates (the tests ran on 3.13.14, but only against the test certificate); macOS may need "Install Certificates.command" (the doctor warns).
- The relevance method is **heuristic**: word and phrase counting with plural folding only ("built" is not "build"; irregular plurals such as "mice" and "knives" are not folded; folding is meant to make a word and its plural share one stem, so it also makes `indie` match `Indy`). The stop words, tone list and kind cues are **English**; other languages work for interest matching, without tone or kind cues. Spaceless scripts match as substrings.
  Tone filtering is a visible word list, not sentiment analysis.
- `kind` is a heuristic; unlabelled sources are `unverified`. Questions are keywords, not questions answered. The package cannot judge truth, only attribute and date.
- **Undated items are never used.** A page without a date cannot be treated as new; feeds are the reliable source. A page's HTTP `Last-Modified` is used only when it is more than two minutes old (a "modified" time of right now means a dynamic page).
  A feed whose dates are consistently wrong (for example a mislabelled zone) will have items refused until they are no longer in the future, or look old.
- Items are built from what the feed or page itself says. Article pages behind a feed entry are **not fetched**, so a title-only feed yields headlines with empty extracts. `meta refresh` redirects and JavaScript-built pages are not followed or run.
  Hidden text is dropped by inline rules and common class names; rules in external style sheets, and text coloured like its background, cannot be seen. A title or summary written with angle brackets is kept as written unless it holds real HTML; RSS titles that really contain HTML tags are read as HTML.
  Implied end tags follow the common cases of the browser rules (paragraphs, list items, definitions, table cells and rows and sections, options), not the whole HTML5 algorithm.
- Feeds are read leniently only for the common slips listed in section 7; any other malformed XML, and any document type declaration, is refused. Content types outside the allow-list (for example `application/octet-stream`, which some storage hosts use for feeds) are refused.
- `robots.txt` is not consulted (the sources are chosen subscriptions, read like a feed reader would). No cookies or logins, so paywalled or private feeds cannot be read; no HTTP/2, Brotli or proxy support; only the standard secure port.
- A daily cap counts every request, including redirect hops, per UTC day. Failed runs retry twice at 20 minute spacing. Rate limiting is per host across all researchers; a sleeping wait is never longer than 30 seconds, and it is not counted against the request deadline.
- One scheduler thread: a very slow source delays other researchers by up to its 20 second deadline plus the polite waits before each of its (at most four) requests, 30 seconds each at the very most with the longest setting (and a researcher's run by up to 180 seconds between sources). The system's name lookup cannot be cancelled; a stuck lookup keeps one helper thread (at most 2) until the operating system gives up.
- Freshness depends on the computer's clock; the doctor compares it with websites' `Date` headers but cannot fix it. Review is per brief, not per item.
- Anything allowed to call `research.save` can make the station request an address it chooses on the public web (never a private one, never over plain http), carrying any data that is in the address itself. This is inherent to user-chosen sources; it is bounded by the guard, the per-host gap and the daily cap, and chat-model calls need approval.
- The database should be copied only while the service is stopped. The status snapshot's `requests_today` is refreshed when state changes, so it can lag by a few requests.
- Rights: the station reads short verbatim extracts with attribution; whether that is permitted for a given source is the operator's responsibility. No rights acknowledgement is implemented in this lane.

## 10. Changes made after the independent reviews

Two reviewers found one blocker, five major issues and a set of minor ones. Each fix has a test that fails on the code as it was and passes now (the reviewers' own probes were reproduced first), and the tests that only needed to be sharper were checked by changing the code on purpose in a private copy (67 deliberate changes to the code, such as keeping the oldest rows instead of the newest or putting a slow pattern back; every one is noticed by at least one test). Behaviour that other lanes or the screen can see:

| What | Change |
| --- | --- |
| A style attribute full of `/*` took minutes to read and froze every thread (blocker) | The comment scan is a plain linear scan. A style of 80,000 characters is read in milliseconds and other threads keep running. The time limit is documented as checked between 16 KB slices. |
| A `Location` such as `//[`, or a page's `<link rel="canonical" href="http://[bad">`, threw away the whole run | Both are ordinary per-source failures or are ignored; `_read_source` also turns any unexpected error into "<host> sent something the station could not make sense of, so it was skipped." for that source only. |
| `GET /briefs?airable=1` missed ready updates behind newer "nothing new" runs | The filter is part of the database query (`service.briefs(airable=True)`, `Store.list_briefs(airable_at=...)`). |
| "It will air automatically." | Now "It does not need your review, so it can be used on air." Nothing in this lane puts a brief on air. Same change in the `review` error text, the operation description and the UI contract. |
| A research Brief did not match what the hosts lane reads | The hosts lane maps the Brief itself (`brief_from_research`) and asks to be handed it as it is; `unverified` is read as `uncertain`. This lane adds tests that run real briefs through that reader, and no second mapping (section 2). |
| Retention and `next_airable` followed the clock | They follow insertion order, so a clock set back cannot delete the brief a run has just made. |
| A polite wait counted against the request deadline; `host_min_interval_seconds` allowed up to 120 | The deadline moves on by the wait; the setting is limited to 1 to 30. |
| One source per run was capped at two items even after the others ran out | The cap is lifted when nothing else is left (section 2). |
| Plural folding was lopsided (`indie`/`indies`, `hero`/`heroes`, `house`/`houses`, `analysis`/`analyses`, `quiz`/`quizzes`) | A word and its plural share one stem. |
| RSS 2.0 in a default namespace was rejected | It is read. |
| 400 unclosed `<p>` or `<li>` were refused as nested too deeply | Elements whose end tag may be left out are closed by the next sibling, never past a hidden element. |
| Invisible characters (the variation-selector supplement and others) survived cleaning | Removed; `neutralize_speech` removes them too. |
| `List<String>` lost its brackets | Only real HTML is read as HTML (section 2). |
| A document type declaration was refused only after the parser had seen up to 64 KiB | Refused before the parser is given a byte; message reworded in plain language. |
| `check_brief` was looser than this document | Item ids, links, categories, source formats, skip reasons and the anchor are checked. |
| Dead addresses next to working ones turned the "Reaching the web" check red | Connection problems count only when the run reached no website. |
| 50 researchers with emoji names overflowed the status limit | Names in the status are cut to 40 characters. |
| A source address with `?token=...` was described as holding no credential | The text is corrected and `research.save` warns (section 3). |
| The grounding lint was quadratic on long scripts | Linear, and only the first 20,000 characters are examined word by word. |
| "Unknown setting: x" | "The setting “x” is not recognised. Check the spelling, or leave it out." |

### Second round: calls that held the interpreter lock

A recheck found that the previous round's HTML-or-plain-text gate (`looks_like_html`) ran one regular-expression search over up to 600,000 characters. The pattern is linear but tries about 110 element names at every `<`,
so one hostile feed field held the interpreter lock for over half a second, long enough for the playout clock (which gives up catching up after 0.5 s) to skip on air. Every call on feed-controlled text was then measured
the same way: a thread ticking every 5 ms, its longest wait while the call ran (best of two runs, CPython 3.12.3, a machine also running the live station, every run under `nice -n 19`).

| Call and hostile input | Longest wait before | After |
| --- | --- | --- |
| `parse_feed`, RSS with a CDATA description of 2 MiB of `<` | 534 ms | 20 ms |
| `parse_feed`, a description of 300,000 `</` | 557 ms | 21 ms |
| `parse_feed`, a title and a description of 600,000 `<` each | 560 ms | 20 ms |
| `parse_feed`, JSON Feed summary of 2 MiB of `<` | 502 ms | 19 ms |
| `parse_feed`, 2 MiB of bare `&` (the repair path) | 199 ms | 15 ms |
| `parse_feed`, 1 MiB of `& ` (repair) | 145 ms | 20 ms |
| `parse_feed`, 2 MiB of control characters (repair) | 221 ms | 13 ms |
| `parse_feed`, 2 MiB of blanks after the XML line | 166 ms | 11 ms |
| `decode_text`, 2 MiB of UTF-16 with a byte order mark and broken pairs | 153 ms | 15 ms |
| `looks_like_html` on 600,000 `<`; `readable_text` on 2 MiB of `<` | 551 ms; 546 ms | 20 ms; 19 ms |
| `clean_text`, 2 MiB of zero-width spaces | 113 ms | 13 ms |
| Already fine, unchanged: JSON `content_html` of 2 MiB `<p>` (the HTML reader already worked in slices), 350,000 `&nbsp;`, 8 categories of 250,000 characters, a 2 MiB link or date, 300 items of 6,000 `<`, `parse_date`, `canonical_key`, `fold` (33 ms) | 10 to 33 ms | 8 to 33 ms |
| `neutralize_speech` on 2 MB of `<\|` (only ever given at most 700 characters, 0.17 ms) | 350 ms | 344 ms, not changed |

Changes: `htmltext.looks_like_html` searches 16 KB windows with a 64 character overlap and counts a match only if it starts inside its window, so a match or its look-ahead is never cut off by a window edge;
`feeds._skip_prolog_space` replaces the single blank-skipping match; `feeds._repair` repairs 64 KB pieces cut just before an `&` (or where the last 40 bytes hold none); `feeds.decode_text` decodes UTF-16 and UTF-32 in slices with the
byte-order decoders, which leave a cut pair for the next slice (Python's incremental `utf-16` decoder was tried first and rejected: it refuses text with no byte order mark, which a one-shot decode accepts); `textutil.clean_text` cleans
text over 64 KB a slice at a time and, with a limit, stops once it holds more than enough to cut the same way. Total work is unchanged or smaller (`parse_date` on 2 MB fell from 109 to 4 ms because it stops early).

Tests (`tests/test_research_feeds.py`): `InterpreterLockTests` (8 tests, the four inputs the recheck named plus repair, blanks, UTF-16 and long text; the other thread's longest wait must stay under 0.1 s, with up to three tries for a busy machine)
and `SlicedWorkGivesTheSameAnswerTests` (5 tests: each sliced call against one pass of the original code on every position across several shrunken slice edges, plus random text; 432,000 random decoding cases were also compared
once outside the suite, with no difference). Checked by changing the code on purpose in a private copy: 12 changes (a match cut at a window edge accepted, a byte order mark cut at a window edge ending the skip, repair cut anywhere,
no blank merged at a slice edge, cleaning stopped too early, the byte order mark ignored, the incremental decoder, and each call put back to one pass); all 12 were noticed.

### Last measured results

Whole research set, `python3 -m unittest discover -s tests -p "test_research*.py"`, CPython 3.12.3, after the review fixes: 447 tests, 0 failures, 0 skipped, about 21 seconds when the machine was quiet (three runs in a row: 21.5, 22.1 and 20.8 seconds; user CPU about 3.4 seconds; most of the wall time is real timeouts and thread joins that the tests deliberately exercise) and up to 28 seconds when other jobs were running. In developer mode (`-X dev`) the same 447 tests ran with no `ResourceWarning` or other warning. No test was removed, skipped or loosened: the original assertions that quoted a message that was deliberately reworded now quote the new wording (the document type refusal, "It will air automatically.", "Unknown setting"), two lists in existing tests only gained entries, and 72 tests were added.
`test_extensions*.py` (the seam): 45 tests, 0 failures.
After the second fix round (section 10, the interpreter lock): 460 tests, 0 failures, 0 skipped on CPython 3.12.3 (25.5 s), 3.11.15 (26.8 s) and the repository's 3.13.14 (26.4 s), every run under `nice -n 19` beside the live station. Before that round CPython 3.11 and 3.13 had last been run on the 375 tests that existed then (0 failures; 3.13 with `-X dev`, no warnings). The fixer used no interpreter outside the repository.
Whole project suite after the second fix round (2026-11-06, run once at the end): 2,817 tests in 335 seconds, 0 failures, 1 skip, 10 expected failures (the soak lane's recorded engine defects).
Whole project suite, run once at the end with every lane's tests present: 2,800 tests in 352 seconds, 0 failures, 1 skip, 10 expected failures. An earlier whole-suite run in the same session, made while three other suites were running on the machine, had one failure in another lane (`test_generation_engine.QueueRulesTests.test_unpinned_seeds_change_between_attempts_and_pinned_seeds_do_not`, a job that was still `queued`); it did not recur in the final run and is not in this lane's files.
Sharper tests: 67 deliberate changes to the code (retention keeping the oldest rows, a diversity limit taken out, a slow pattern put back, an unlabelled source treated as reporting, and so on) were each made in a private copy and the lane's tests run against it; all 67 were noticed. Before the fixes, 16 of the 18 changes the reviewer tried had gone unnoticed.
Code: 4,832 lines under `myradio/research/` (`wc -l`); tests: 5,593 lines under `tests/test_research*.py`, including 440 lines of shared fixtures; this document: about 460 lines.
