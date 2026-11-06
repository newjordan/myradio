"""Map the three station sliders to the library's own filters.

The Sound step of the studio has three sliders, each a whole number from 0 to 100:

* **Energy** (Calm to Lively) chooses the style's energy class and the texture (density).
* **How adventurous?** (Familiar to Surprising) chooses how elaborate the writing is
  (complexity) and how widely the station rotates through styles before repeating one.
* **Vocals** (Instrumental to With lyrics) chooses how often a song is sung rather than instrumental.

The mapping is a plain table, so a person, an agent and a test all read it the same way:

    energy             class     density       adventurousness     complexity   avoids repeating
    0 to 33            calm      sparse        0 to 33             simple       the last 2 styles
    34 to 66           steady    balanced      34 to 66            layered      the last 6 styles
    67 to 100          driving   lush          67 to 100           intricate    the last 12 styles

    vocals 0 means never sung and 100 means always sung; in between, each song is sung with that
    percentage chance, decided from the seed so the same seed always gives the same answer.

**Complex never means loud.** Complexity comes only from the adventurousness slider. The energy
class and the texture come only from the energy slider. Moving one slider never changes what the
other one chose, and "lush" texture is about fuller registers, not volume (see the density text in
the compiled prompt).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# (lowest, highest, value) bands, inclusive.
ENERGY_BANDS = ((0, 33, "calm"), (34, 66, "steady"), (67, 100, "driving"))
DENSITY_BY_ENERGY = {"calm": "sparse", "steady": "balanced", "driving": "lush"}
COMPLEXITY_BANDS = ((0, 33, "simple"), (34, 66, "layered"), (67, 100, "intricate"))
# How many recent styles / arrangement designs / presets a station avoids repeating.
STYLE_WINDOW = {"simple": 2, "layered": 6, "intricate": 12}
ARRANGEMENT_WINDOW = {"simple": 1, "layered": 3, "intricate": 5}
PRESET_WINDOW = {"simple": 100, "layered": 200, "intricate": 400}

_ENERGY_WORDS = {"calm": "Calm and spacious", "steady": "Easygoing and balanced", "driving": "Lively and full"}
_ADVENTURE_WORDS = {
    "simple": "familiar, clearly shaped tunes",
    "layered": "a few surprises",
    "intricate": "surprising, richly layered writing",
}


def _band(value: int, bands) -> str:
    for low, high, name in bands:
        if low <= value <= high:
            return name
    raise ValueError("slider value out of range")  # unreachable after validation


def _slider(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 100:
        raise ValueError(f"{name} must be a whole number from 0 to 100")
    return value


def vocal_words(vocals: int) -> str:
    if vocals == 0:
        return "instrumental only"
    if vocals <= 33:
        return "mostly instrumental"
    if vocals <= 66:
        return "a mix of instrumentals and songs with words"
    if vocals < 100:
        return "mostly songs with words"
    return "always songs with words"


@dataclass(frozen=True)
class SliderFilters:
    energy: int
    adventurousness: int
    vocals: int
    energy_class: str       # filter for the style's energy: calm, steady or driving
    density: str            # texture: sparse, balanced or lush
    complexity: str         # simple, layered or intricate
    vocal_share: float      # 0.0 (never sung) to 1.0 (always sung)
    style_window: int       # how many recent styles to avoid repeating
    arrangement_window: int
    preset_window: int

    @property
    def modes(self) -> tuple[str, ...]:
        if self.vocals == 0:
            return ("instrumental",)
        if self.vocals == 100:
            return ("vocal",)
        return ("instrumental", "vocal")

    def mode_for(self, seed: int) -> str:
        """The mode for one song. Raising the vocals slider only ever turns instrumentals into songs."""
        if self.vocals == 0:
            return "instrumental"
        if self.vocals == 100:
            return "vocal"
        draw = random.Random(f"mode:{seed}").random()
        return "vocal" if draw < self.vocal_share else "instrumental"

    def browse_filters(self) -> dict:
        """Keyword arguments for ``Library.browse`` and ``Library.choose``."""
        return {"energy": self.energy_class}

    def summary(self) -> str:
        return f"{_ENERGY_WORDS[self.energy_class]}; {_ADVENTURE_WORDS[self.complexity]}; {vocal_words(self.vocals)}"

    def to_dict(self) -> dict:
        return {
            "energy": self.energy, "adventurousness": self.adventurousness, "vocals": self.vocals,
            "energy_class": self.energy_class, "density": self.density, "complexity": self.complexity,
            "vocal_share": self.vocal_share, "modes": list(self.modes),
            "avoid_repeating": {"styles": self.style_window, "arrangements": self.arrangement_window,
                                "presets": self.preset_window},
            "summary": self.summary(),
        }


def filters_from_sliders(energy: int, adventurousness: int, vocals: int) -> SliderFilters:
    """Turn the three sliders (whole numbers 0 to 100) into library filters. See the module table."""
    energy = _slider(energy, "energy")
    adventurousness = _slider(adventurousness, "adventurousness")
    vocals = _slider(vocals, "vocals")
    energy_class = _band(energy, ENERGY_BANDS)
    complexity = _band(adventurousness, COMPLEXITY_BANDS)
    return SliderFilters(
        energy=energy, adventurousness=adventurousness, vocals=vocals,
        energy_class=energy_class, density=DENSITY_BY_ENERGY[energy_class], complexity=complexity,
        vocal_share=vocals / 100, style_window=STYLE_WINDOW[complexity],
        arrangement_window=ARRANGEMENT_WINDOW[complexity], preset_window=PRESET_WINDOW[complexity],
    )
