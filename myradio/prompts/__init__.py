"""Structured music prompts. DJ and anchor speech belongs to Drama 3."""

from .compat import RULES, Finding, check_arrangement, check_library, check_palette, check_preset
from .library import (
    COMPLEXITIES,
    DENSITIES,
    ENERGIES,
    MODES,
    MUSIC_MODEL,
    CatalogError,
    Library,
    NoMatchingPreset,
    Preset,
    SongPrompt,
    filter_presets,
    load_library,
    replay,
    shared_library,
)
from .lyrics import LyricScaffold, contains_dj_speech, lyrics_scaffold
from .service import PromptError, PromptService
from .sliders import SliderFilters, filters_from_sliders
from .store import PromptStore, StoreError
from .userpresets import UserPresetError

__all__ = [
    "COMPLEXITIES", "DENSITIES", "ENERGIES", "MODES", "MUSIC_MODEL", "RULES",
    "CatalogError", "Finding", "Library", "LyricScaffold", "NoMatchingPreset", "Preset", "PromptError",
    "PromptService", "PromptStore", "SliderFilters", "SongPrompt", "StoreError", "UserPresetError",
    "check_arrangement", "check_library", "check_palette", "check_preset", "contains_dj_speech",
    "filter_presets", "filters_from_sliders", "load_library", "lyrics_scaffold", "replay", "shared_library",
]
