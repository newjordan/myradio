"""CLI for browsing, generating, and exporting music prompt records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compat import check_library
from .library import (
    COMPLEXITIES, DENSITIES, ENERGIES, MAX_LYRICS_CHARS, MAX_SEED,
    Library, load_library,
)
from .lyrics import FORMS, TAG_STYLES, lyrics_scaffold
from .sliders import filters_from_sliders


def _seed_arg(value: str) -> int:
    try:
        seed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("seed must be an integer") from exc
    if not 0 <= seed <= MAX_SEED:
        raise argparse.ArgumentTypeError(f"seed must be in 0..{MAX_SEED}")
    return seed


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--family")
    parser.add_argument("--tag", action="append", default=[], help="Require this tag; repeat for AND")
    parser.add_argument("--exclude-tag", action="append", default=[])
    parser.add_argument("--energy", choices=ENERGIES)
    parser.add_argument("--query", default="", help="Case-insensitive search; all words must match")


def _add_generation(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", type=_seed_arg, default=0)
    parser.add_argument("--complexity", choices=COMPLEXITIES, default="layered")
    parser.add_argument("--density", choices=DENSITIES, default="balanced")


def _filters(args: argparse.Namespace) -> dict:
    return {
        "family": args.family, "tags": args.tag,
        "exclude_tags": args.exclude_tag, "energy": args.energy, "query": args.query,
    }


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _slider_arg(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("sliders are whole numbers from 0 to 100") from exc
    if not 0 <= number <= 100:
        raise argparse.ArgumentTypeError("sliders are whole numbers from 0 to 100")
    return number


def _validate(library: Library) -> dict:
    count = 0
    for preset in library.presets:
        for complexity in COMPLEXITIES:
            for density in DENSITIES:
                for mode in ("instrumental", "vocal"):
                    library.generate(
                        preset.id, 0, complexity=complexity, density=density,
                        mode=mode, lyrics="[Verse]\nA new day opens wide." if mode == "vocal" else None,
                    )
                    count += 1
    rules = check_library(library)
    return {
        "valid": rules["ok"], "catalog_version": library.version,
        "palettes": len(library.palettes), "arrangements": len(library.arrangements),
        "presets": len(library.presets), "compiled_variants": count,
        "compatibility": {"ok": rules["ok"], "errors": len(rules["errors"]), "warnings": len(rules["warnings"]),
                          "rules": rules["rules"]},
        "audio_evaluated": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="myradio's offline, music-only word-salad library")
    parser.add_argument("--catalog", metavar="VERSION", help="Use the catalog as it stood at this version (for example 1.0.0); default is the newest")
    commands = parser.add_subparsers(dest="command", required=True)

    browse = commands.add_parser("list", help="List matching presets as a JSON array")
    _add_filters(browse)

    generate = commands.add_parser("generate", help="Generate one reproducible song-prompt JSON record")
    _add_filters(generate)
    _add_generation(generate)
    generate.add_argument("--preset", help="Exact stable preset ID; filters still apply")
    generate.add_argument("--recent", action="append", default=[], help="Exclude a recent preset ID")
    generate.add_argument("--recent-palette", action="append", default=[], help="Exclude a recent style palette")
    generate.add_argument("--lyrics-file", type=Path, help="UTF-8 sung lyrics; enables vocal music mode, not DJ speech")
    generate.add_argument("--scaffold-lyrics", action="store_true",
                          help="Vocal music with a generated lyric outline instead of your own lyrics (not with --lyrics-file)")

    export = commands.add_parser("export", help="Export matching instrumental prompts as JSON Lines")
    _add_filters(export)
    _add_generation(export)

    commands.add_parser("validate", help="Validate catalog structure, compatibility rules and compile every variant (no audio)")
    commands.add_parser("check", help="Run the compatibility rules over the catalog; exit 1 when a rule is broken")

    lyrics = commands.add_parser("lyrics", help="Write a suggested lyric outline for a preset (not your own lyrics, never DJ speech)")
    lyrics.add_argument("--preset", required=True, help="Exact stable preset ID")
    lyrics.add_argument("--seed", type=_seed_arg, default=0)
    lyrics.add_argument("--tag-style", choices=TAG_STYLES, default="title")
    lyrics.add_argument("--form", choices=FORMS)
    lyrics.add_argument("--text", action="store_true", help="Print only the lyrics text")

    sliders = commands.add_parser("sliders", help="Show what the three studio sliders (0 to 100) choose")
    sliders.add_argument("--energy", type=_slider_arg, required=True)
    sliders.add_argument("--adventurousness", type=_slider_arg, required=True)
    sliders.add_argument("--vocals", type=_slider_arg, required=True)

    args = parser.parse_args(argv)

    try:
        library = load_library(through=args.catalog)
        if args.command == "validate":
            report = _validate(library)
            _emit(report)
            if not report["valid"]:
                return 1
        elif args.command == "check":
            report = check_library(library)
            _emit(report)
            if not report["ok"]:
                return 1
        elif args.command == "lyrics":
            scaffold = lyrics_scaffold(library.get(args.preset), args.seed, tag_style=args.tag_style, form=args.form)
            if args.text:
                print(scaffold.text)
            else:
                _emit(scaffold.to_dict())
        elif args.command == "sliders":
            _emit(filters_from_sliders(args.energy, args.adventurousness, args.vocals).to_dict())
        elif args.command == "list":
            _emit([p.to_dict() for p in library.browse(**_filters(args))])
        elif args.command == "export":
            presets = library.browse(**_filters(args))
            if not presets:
                raise ValueError("No presets match the export filters")
            # Compile first so invalid input cannot leave a partial export on stdout.
            prompts = [library.generate(
                p.id, args.seed, complexity=args.complexity, density=args.density,
            ).to_dict() for p in presets]
            for prompt in prompts:
                _emit(prompt)
        else:
            lyrics = None
            if args.lyrics_file is not None and args.scaffold_lyrics:
                raise ValueError("Use either your own lyrics (--lyrics-file) or a generated outline (--scaffold-lyrics), not both")
            if args.lyrics_file is not None:
                with args.lyrics_file.open(encoding="utf-8") as source:
                    lyrics = source.read(MAX_LYRICS_CHARS + 1)
                if len(lyrics) > MAX_LYRICS_CHARS:
                    raise ValueError(f"Lyrics exceed {MAX_LYRICS_CHARS} characters")
            if args.preset is not None:
                preset = library.get(args.preset)
                if preset not in library.browse(**_filters(args)):
                    raise ValueError("The requested preset conflicts with the supplied filters")
                if (preset.id in {v.strip().lower() for v in args.recent}
                        or preset.palette.id in {v.strip().lower() for v in args.recent_palette}):
                    raise ValueError("The requested preset is excluded by recent history")
            else:
                preset = library.choose(
                    args.seed, recent_ids=args.recent, recent_palettes=args.recent_palette,
                    **_filters(args),
                )
            if args.scaffold_lyrics:
                lyrics = lyrics_scaffold(preset, args.seed).text
            _emit(library.generate(
                preset.id, args.seed, complexity=args.complexity, density=args.density,
                mode="vocal" if lyrics is not None else "instrumental", lyrics=lyrics,
            ).to_dict())
    except (OSError, UnicodeError, ValueError) as exc:
        parser.exit(2, f"myradio-prompts: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
