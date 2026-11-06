"""Interest-first research: user-owned researchers that read chosen sources and leave sourced briefs.

Start with ``myradio.research.extension.build()`` (the station does this). The pieces can also be used on their own:

* ``fetcher.Fetcher``: the safe, bounded HTTPS client.
* ``feeds.parse_feed`` / ``htmltext.parse_html``: RSS, Atom, JSON Feed and page readers.
* ``selection.select_items``: relevance, exclusions, freshness, de-duplication, diversity and the tone filter.
* ``service.ResearchService``: the scheduler, run pipeline, review queue and the API the hosts and director use.
* ``grounding.check_script``: an advisory lint that a spoken script stays inside the facts of a brief.
* ``schema.check_brief``: the Brief document as executable checks.

See ``docs/lanes/research.md`` for the contract, including the Brief schema.
"""
from .errors import FetchError
from .feeds import FeedError, parse_feed
from .fetcher import FetchResult, Fetcher
from .service import ResearchService

__all__ = ["FeedError", "FetchError", "FetchResult", "Fetcher", "ResearchService", "parse_feed"]
