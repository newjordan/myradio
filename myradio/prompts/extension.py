"""The music-style library as a station extension.

Operations (typed, validated by the host before they run):

* ``prompts.favorite``      mark or unmark a style or preset as a favourite
* ``prompts.save_custom``   save the person's own style (a copy, an edit or a new one)
* ``prompts.remove_custom`` remove a style the person saved

Routes (all under ``/api/x/prompts``, authenticated like the rest of the studio):

* ``GET  /catalog``  paged, filterable list of styles, presets or arrangement designs
* ``GET  /search``   search styles, saved styles and arrangements by words
* ``POST /compose``  choose a style (or use the one named), write the prompt, return it with its id
* ``GET  /sliders``  what a set of slider positions means
* ``GET  /lyrics``   a suggested lyric outline for a preset and seed (nothing is remembered)

This extension never starts a model, makes audio or touches the network. State lives in
``<data>/ext/prompts/prompts.sqlite3`` only. Core playout never calls into it.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from myradio.extensions import Arg, Context, Extension, ExtensionError, Operation, Request, Route

from .compat import check_library_cached
from .library import MAX_SEED, shared_library
from .service import PromptError, PromptService
from .store import PromptStore, StoreError
from .userpresets import shown_names

_CATALOG_PARAMS = {"view", "family", "tag", "exclude_tag", "energy", "q", "source", "favorites", "sort", "offset", "limit"}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _one(query: dict, key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    if len(values) > 1:
        raise ExtensionError(f"Give '{key}' only once.")
    return values[0]


def _int(query: dict, key: str, default: int | None, low: int, high: int) -> int | None:
    raw = _one(query, key)
    if raw is None:
        return default
    if not re.fullmatch(r"\d{1,19}", raw) or not low <= int(raw) <= high:
        raise ExtensionError(f"'{key}' must be a whole number from {low} to {high}.")
    return int(raw)


def _bool(query: dict, key: str) -> bool:
    raw = _one(query, key)
    if raw is None:
        return False
    if raw.lower() in _TRUE:
        return True
    if raw.lower() in _FALSE:
        return False
    raise ExtensionError(f"'{key}' must be true or false.")


def _reject_unknown(query: dict, allowed: set[str]) -> None:
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ExtensionError(f"These options are not recognised: {shown_names(unknown)}. Allowed: {', '.join(sorted(allowed))}.")


def _guard(call):
    """Turn the service's presentable failures into the host's presentable failure."""
    try:
        return call()
    except (PromptError, StoreError) as exc:
        raise ExtensionError(str(exc)) from None


class PromptsExtension(Extension):
    name = "prompts"

    def __init__(self) -> None:
        self.service: PromptService | None = None
        self._store: PromptStore | None = None
        self._status: dict = {"ready": False, "error": "Not started."}
        self._open_problem: str | None = None  # why the saved-styles file could not be opened (until restart)
        self._rules: dict = {"ok": True, "errors": [], "warnings": []}

    # ---- lifecycle ----------------------------------------------------------------------
    def start(self, ctx: Context) -> None:
        library = shared_library()  # a broken catalog fails the extension visibly rather than half-working
        try:
            self._store = PromptStore(ctx.open_db("prompts.sqlite3"), clock=ctx.clock)
        except (ExtensionError, StoreError, sqlite3.Error, OSError) as exc:
            self._store = None
            self._open_problem = str(exc) if isinstance(exc, (ExtensionError, StoreError)) else "The saved-styles file could not be opened."
        # The service tells us after every change (a saved style, a favourite, a remembered compose), so the
        # status stays right even when other code calls PromptService.compose directly.
        self.service = PromptService(self._store, library=library, clock=ctx.clock, on_change=self._refresh)
        report = check_library_cached(library)
        self._rules = {"ok": report["ok"], "errors": report["errors"][:5], "warnings": len(report["warnings"]),
                       "rules": report["rules"]}
        self._refresh()

    def stop(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def _refresh(self) -> None:
        """Rebuild the cached status. The service calls this after anything that changes counts; status() never does."""
        service = self.service
        if service is None:
            return
        problem = self._open_problem
        try:
            snapshot = service.snapshot()
        except StoreError as exc:  # a passing trouble: the next refresh clears it once the file answers again
            snapshot = {**{k: 0 for k in ("saved_styles", "favorites", "remembered")}, "store_available": False,
                        "damaged": [], "catalog_version": service.library.version, "styles": len(service.library.palettes),
                        "arrangements": len(service.library.arrangements), "presets": len(service.library.presets),
                        "last_composed": None}
            problem = str(exc)
        self._status = {"ready": True, "error": problem, **snapshot, "rules_ok": self._rules["ok"]}

    def status(self) -> dict:
        return self._status  # a cached snapshot: O(1), no locks, no station.status()

    def persistent_files(self) -> list[str]:
        return ["prompts.sqlite3"]

    # ---- doctor checks ------------------------------------------------------------------
    def checks(self) -> list[dict]:
        status = self._status
        service = self.service
        if service is None:
            return []
        out = [{
            "id": "catalog", "title": "Music styles", "status": "ok",
            "detail": f"{status['styles']} styles and {status['arrangements']} arrangement designs are ready "
                      f"({status['presets']:,} combinations).",
            "hint": "",
        }]
        if self._rules["ok"]:
            out.append({"id": "rules", "title": "Style rules", "status": "ok",
                        "detail": "Every built-in style passes the checks for instruments that belong together and consistent tempo and meter.",
                        "hint": ""})
        else:
            out.append({"id": "rules", "title": "Style rules", "status": "warn",
                        "detail": f"{len(self._rules['errors'])} built-in style(s) break a rule, for example: {self._rules['errors'][0]['message']}",
                        "hint": "This is a fault in the installed catalog. Update myradio."})
        if status.get("store_available"):
            damaged = status.get("damaged") or []
            if damaged:
                out.append({"id": "saved-styles", "title": "Your saved styles", "status": "warn",
                            "detail": f"{len(damaged)} saved style(s) could not be read and were left untouched: {', '.join(damaged[:5])}.",
                            "hint": "Save those styles again, or restore your data folder from a backup."})
            else:
                out.append({"id": "saved-styles", "title": "Your saved styles", "status": "ok",
                            "detail": f"{status['saved_styles']} saved style(s) and {status['favorites']} favourite(s).", "hint": ""})
            count = status["remembered"]
            detail = ("The repeat memory is empty. It fills as songs are composed, so the station does not repeat itself."
                      if not count else
                      f"Remembering the last {count:,} composed prompt{'s' if count != 1 else ''} (up to 1,000), "
                      "so the station does not repeat itself.")
            out.append({"id": "repeat-memory", "title": "Repeat memory", "status": "ok", "detail": detail, "hint": ""})
        else:
            out.append({"id": "saved-styles", "title": "Your saved styles", "status": "warn",
                        "detail": "Saved styles, favourites and the repeat memory are unavailable right now. The built-in styles still work.",
                        "hint": "Check that the data folder can be written to, then restart myradio."})
        return out

    # ---- operations ---------------------------------------------------------------------
    def operations(self) -> list[Operation]:
        def favorite(ctx: Context, args: dict) -> dict:
            return _guard(lambda: self._service().favorite(args["id"], args["on"]))

        def save_custom(ctx: Context, args: dict) -> dict:
            return _guard(lambda: self._service().save_custom(args["preset"]))

        def remove_custom(ctx: Context, args: dict) -> dict:
            return _guard(lambda: self._service().remove_custom(args["id"]))

        return [
            Operation(
                "favorite",
                "Mark a music style or preset as a favourite, or take the mark off. Use a style id such as "
                "warm-brush-jazz, a preset id such as warm-brush-jazz/theme-and-return, or user:<name> for a saved style.",
                {"id": Arg("string", "Style, preset or saved-style id.", max_length=120),
                 "on": Arg("boolean", "true to mark as a favourite, false to remove the mark.")},
                favorite),
            Operation(
                "save_custom",
                "Save your own music style: a copy of a built-in one with changes (give based_on), or a new one. "
                "It is checked with the same rules as the built-in styles and saved as user:<id>.",
                {"preset": Arg("object", "{id, title?, based_on?, palette, arrangement}. Palette and arrangement follow the catalog schema.")},
                save_custom),
            Operation(
                "remove_custom",
                "Remove a music style you saved yourself. Its id starts with user:. Built-in styles cannot be removed.",
                {"id": Arg("string", "The saved style's id, for example user:evening-porch.", max_length=80)},
                remove_custom),
        ]

    def _service(self) -> PromptService:
        if self.service is None:
            raise ExtensionError("The music style library is not running.")
        return self.service

    # ---- routes -------------------------------------------------------------------------
    def routes(self) -> list[Route]:
        def catalog(ctx: Context, req: Request) -> dict:
            query = dict(req.query)
            _reject_unknown(query, _CATALOG_PARAMS)
            return _guard(lambda: self._service().catalog(
                view=_one(query, "view") or "presets", family=_one(query, "family"),
                tags=[t for t in query.get("tag", [])], exclude_tags=[t for t in query.get("exclude_tag", [])],
                energy=_one(query, "energy"), query=_one(query, "q") or "", source=_one(query, "source") or "all",
                favorites=_bool(query, "favorites"), sort=_one(query, "sort") or "catalog",
                offset=_int(query, "offset", 0, 0, 100_000), limit=_int(query, "limit", 24, 1, 100),
            ))

        def search(ctx: Context, req: Request) -> dict:
            query = dict(req.query)
            _reject_unknown(query, {"q", "limit"})
            return _guard(lambda: self._service().search(_one(query, "q") or "", limit=_int(query, "limit", 10, 1, 25)))

        def sliders(ctx: Context, req: Request) -> dict:
            query = dict(req.query)
            _reject_unknown(query, {"energy", "adventurousness", "vocals"})
            values = {key: _int(query, key, None, 0, 100) for key in ("energy", "adventurousness", "vocals")}
            if any(v is None for v in values.values()):
                raise ExtensionError("Give all three: energy, adventurousness and vocals (whole numbers from 0 to 100).")
            return _guard(lambda: self._service().sliders(**values))

        def lyrics(ctx: Context, req: Request) -> dict:
            query = dict(req.query)
            _reject_unknown(query, {"preset", "seed", "tag_style", "form"})
            preset = _one(query, "preset")
            seed = _int(query, "seed", None, 0, MAX_SEED)
            if not preset or seed is None:
                raise ExtensionError("Give the preset id and a seed, for example ?preset=warm-brush-jazz/theme-and-return&seed=7.")
            return _guard(lambda: self._service().lyrics(
                preset, seed, tag_style=_one(query, "tag_style") or "title", form=_one(query, "form")))

        def compose(ctx: Context, req: Request) -> dict:
            if not isinstance(req.json, dict):
                raise ExtensionError("Send the settings as a JSON object, for example {\"seed\": 7}.")
            return _guard(lambda: self._service().compose(req.json))

        return [
            Route("GET", "/catalog", catalog),
            Route("GET", "/search", search),
            Route("GET", "/sliders", sliders),
            Route("GET", "/lyrics", lyrics),
            Route("POST", "/compose", compose, max_body=131072),
        ]


def build() -> Extension:
    return PromptsExtension()


__all__: list[Any] = ["PromptsExtension", "build"]
