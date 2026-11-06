"""On-air personalities and the script composer.

Hosts are made-up characters. The composer writes the text a speech model will say; it never
makes audio and makes no network call. Every number, name and address in a script comes from
the Facts the caller supplies or from the host's own phrases, and ``grounding_lint`` checks it.

Quick use::

    from myradio.hosts import HostsService
    service = HostsService()                       # no database: nothing is saved
    script = service.compose("nova", "song_intro", {"next_title": "Paper Lantern Weather"}, seed=7)
    script.text, script.speaker                    # what the generation lane needs

See docs/lanes/hosts.md for the full contract.
"""
from .briefs import brief_from_research, compose_update, parse_brief, verbatim_extract
from .composer import ComposerConfig, Script, TemplateComposer
from .errors import HostsError, NothingToRead
from .facts import Facts, number_words, spoken_time
from .formats import FORMATS, compose_format
from .lint import grounding_lint
from .llm import LlmComposer
from .personas import Persona, validate_persona
from .segments import SEGMENTS
from .service import HostsService
from .textsafe import EVENT_TAGS, audit_speech_text, neutralize
from .timing import estimate_seconds

__all__ = [
    "ComposerConfig", "EVENT_TAGS", "FORMATS", "Facts", "HostsError", "HostsService", "LlmComposer", "NothingToRead",
    "Persona", "SEGMENTS", "Script", "TemplateComposer", "audit_speech_text", "brief_from_research", "compose_format",
    "compose_update", "estimate_seconds", "grounding_lint", "neutralize", "number_words", "parse_brief", "spoken_time",
    "validate_persona", "verbatim_extract",
]
