"""Word bank for the lyric scaffold (``lyrics.py``).

Everything here is deliberately gentle, concrete and about things and places, never about a radio
station, a host or an announcement. Pairing nouns, verbs and adjectives from different corners of
a world is what makes a line "surreal but singable": the grammar is plain and every line has the
syllable count the plan asks for.

Syllable counts are the counts a singer would give an English word. They are stored by group (the
word lists are split by syllable count) and a test cross-checks every word against an independent
heuristic, so a typo in a count is caught.
"""

from __future__ import annotations

from dataclasses import dataclass


def _words(text: str) -> tuple[str, ...]:
    return tuple(text.split())


@dataclass(frozen=True)
class World:
    """A small imaginative setting: nouns and adjectives by syllable count, and recurring hooks."""

    id: str
    keywords: tuple[str, ...]            # style words that make this world a better fit
    nouns: dict[int, tuple[str, ...]]
    adjectives: dict[int, tuple[str, ...]]
    motifs: tuple[tuple[str, int, str], ...]  # (hook phrase, syllables, rhyme group of its last word)


WORLDS: tuple[World, ...] = (
    World(
        "harbor-night",
        ("late-night", "nocturnal", "night-drive", "moon", "hypnotic", "dub", "lullaby", "dream", "reflective", "ambient", "hazy"),
        {
            1: _words("moon boat tide dock sail rope wave star bell lamp net mast"),
            2: _words("lantern harbor anchor shadow candle whistle river window ribbon compass mirror pocket blanket"),
            3: _words("lullaby melody horizon umbrella"),
        },
        {
            1: _words("slow deep soft warm calm bright blue small gold wide"),
            2: _words("silver gentle velvet paper golden sleepy rolling drifting glowing humming tender"),
            3: _words("wonderful beautiful unfolding"),
        },
        (
            ("the lantern learns to swim", 6, "im"),
            ("we sail the sleeping tide", 6, "ide"),
            ("the moon forgets the shore", 6, "ore"),
            ("paper boats in the rain", 6, "ain"),
            ("a candle in the bay", 6, "ay"),
            ("the harbor hums a tune", 6, "oon"),
        ),
    ),
    World(
        "orchard-morning",
        ("sunlit", "uplifting", "playful", "pastoral", "dance", "folk", "acoustic", "warm", "hopeful", "joyful", "cafe"),
        {
            1: _words("bee pear plum leaf dew sun gate vine hen row seed bread"),
            2: _words("apple ladder basket orchard blossom meadow morning kettle pillow wagon sparrow fiddle pocket"),
            3: _words("marmalade afternoon butterfly lemonade memory"),
        },
        {
            1: _words("ripe warm sweet green bright soft round gold small fresh"),
            2: _words("golden mellow sunny gentle early rolling dusty rosy tender friendly"),
            3: _words("wonderful marvelous unfolding"),
        },
        (
            ("the apple knows the way", 6, "ay"),
            ("a ladder to the sky", 6, "y"),
            ("the bees are singing home", 6, "ome"),
            ("honey in the morning light", 7, "ight"),
            ("the orchard holds the gold", 6, "old"),
        ),
    ),
    World(
        "workshop-maker",
        ("rhythmic", "interlocking", "sequenced", "minimal", "playful", "groove", "keys", "math-rock", "progressive", "chamber", "counterpoint"),
        {
            1: _words("gear clock nail saw spool thread glue key knot screw wheel lock"),
            2: _words("hammer ribbon needle button pattern compass pencil engine marble paper magnet puzzle kettle"),
            3: _words("telescope invention carpenter microscope mystery"),
        },
        {
            1: _words("small bright warm true round quick neat fine bold new"),
            2: _words("copper silver tiny clever spinning ticking painted woven shiny gentle patient"),
            3: _words("wonderful beautiful unfolding"),
        },
        (
            ("the clock keeps time for the sea", 7, "ee"),
            ("a button for the moon", 6, "oon"),
            ("the needle finds the way", 6, "ay"),
            ("a ribbon round the moon", 6, "oon"),
            ("the gears begin to sing", 6, "ing"),
        ),
    ),
    World(
        "rain-window",
        ("reflective", "tender", "gentle", "soft", "study", "hazy", "cozy", "lo-fi", "meditative", "lyrical", "spacious"),
        {
            1: _words("rain drop pane steam cloud glass wind roof door mug cup quilt"),
            2: _words("window kettle curtain puddle blanket teapot candle pillow shadow mitten slipper garden"),
            3: _words("umbrella afternoon melody lullaby memory"),
        },
        {
            1: _words("soft warm gray calm slow cool deep small bright blue"),
            2: _words("gentle silver patient drowsy misty rolling humming tender woolen"),
            3: _words("wonderful beautiful unfolding"),
        },
        (
            ("the kettle sings to the rain", 7, "ain"),
            ("a window full of sky", 6, "y"),
            ("the rain is knitting light", 6, "ight"),
            ("a puddle for the moon", 6, "oon"),
            ("we wait inside the glow", 6, "ow"),
        ),
    ),
    World(
        "sky-travel",
        ("odd-meter", "exploratory", "progressive", "dance", "heritage-inspired", "night-drive", "hopeful", "adventurous", "motorik", "retro", "playful"),
        {
            1: _words("kite cloud star road map bridge train wing breeze hill gate sail"),
            2: _words("compass ticket valley mountain river meadow lantern comet rocket ladder window postcard pocket suitcase feather journey island tunnel"),
            3: _words("horizon umbrella telescope adventure memory afternoon"),
        },
        {
            1: _words("far wide high bright free long blue small soft cool"),
            2: _words("silver golden distant drifting rolling windy feathered patient eager ready"),
            3: _words("wonderful beautiful unfolding"),
        },
        (
            ("a kite above the town", 6, "own"),
            ("the road is full of light", 6, "ight"),
            ("we follow the singing star", 7, "ar"),
            ("a map to the morning star", 7, "ar"),
            ("the bridge begins to glow", 6, "ow"),
        ),
    ),
)

def _verbs(text: str) -> dict[int, tuple[tuple[str, str], ...]]:
    """Parse "syllables: base/third base/third ..." lines into {syllables: ((base, third), ...)}."""
    result = {}
    for line in text.strip().splitlines():
        count, _, rest = line.partition(":")
        result[int(count)] = tuple(tuple(pair.split("/")) for pair in rest.split())
    return result


# Verbs whose -s form has the same number of syllables as the base form: (base, third person).
# Transitive verbs take an object ("fold the moon"); intransitive verbs stand with a place
# ("drift past the shore"). Keeping them apart is what keeps the grammar plain.
VERBS_T = _verbs("""
1: fold/folds hold/holds mend/mends spin/spins weave/weaves rock/rocks tap/taps lend/lends climb/climbs trade/trades
   wake/wakes paint/paints knit/knits count/counts braid/braids stir/stirs bake/bakes draw/draws save/saves lift/lifts
   sweep/sweeps tie/ties meet/meets greet/greets call/calls name/names show/shows lead/leads bring/brings build/builds
   feed/feeds fill/fills turn/turns light/lights bend/bends keep/keeps ring/rings hum/hums sing/sings play/plays
   swing/swings row/rows shine/shines
2: carry/carries follow/follows borrow/borrows gather/gathers answer/answers water/waters shelter/shelters open/opens
3: remember/remembers imagine/imagines discover/discovers recover/recovers
""".replace("\n   ", " "))
VERBS_I = _verbs("""
1: hum/hums sing/sings drift/drifts dream/dreams glow/glows flow/flows wait/waits play/plays climb/climbs swing/swings
   turn/turns ring/rings shine/shines rock/rocks
2: tumble/tumbles wander/wanders ripple/ripples travel/travels shimmer/shimmers hover/hovers sparkle/sparkles
""".replace("\n   ", " "))

PRONOUNS = ("we", "you", "they")

# Preposition + "the", with syllables.
PREPOSITIONS: dict[int, tuple[str, ...]] = {
    2: ("in the", "on the", "by the", "to the", "for the", "past the", "from the", "through the"),
    3: ("under the", "across the", "along the", "beside the", "toward the", "beneath the", "above the",
        "behind the", "around the", "between the", "inside the", "over the", "beyond the"),
}

# Rhyme groups. Each entry is word:pos[:syllables] where pos is any of n (noun), a (adjective),
# v (verb, base form). One syllable unless a count is given. Every word in a group rhymes with the others.
_RHYMES = {
    "im": "swim:v dim:a brim:n rim:n whim:n",
    "ide": "tide:n wide:a glide:v ride:v guide:n side:n slide:v hide:v pride:n stride:nv",
    "ore": "shore:n door:n floor:n core:n oar:n roar:nv store:n soar:v ashore:a:2",
    "ain": "rain:n train:n lane:n chain:n grain:n plane:n remain:v:2 plain:a main:a",
    "ay": "day:n way:n bay:n ray:n tray:n hay:n gray:a play:nv stay:v sway:nv pray:v",
    "oon": "moon:n tune:n spoon:n noon:n balloon:n:2 croon:v cocoon:n:2 lagoon:n:2 afternoon:n:3",
    "un": "sun:n run:v done:a begun:a:2",
    "ome": "home:n roam:v foam:n dome:n comb:n",
    "ight": "night:n light:na kite:n flight:n sight:n bright:a white:a right:a ignite:v:2 delight:nv:2 tonight:n:2",
    "old": "gold:na old:a cold:a fold:n bold:a unfold:v:2",
    "ee": "sea:n tree:n key:n bee:n tea:n free:a agree:v:2 flee:v",
    "ing": "sing:v ring:nv spring:n wing:n string:n swing:nv cling:v",
    "y": "sky:n high:a fly:v sigh:nv tie:n eye:n pie:n dry:a shy:a lullaby:n:3 butterfly:n:3",
    "ow": "glow:nv flow:nv slow:a low:a grow:v snow:n window:n:2 aglow:a:2 below:a:2",
    "own": "town:n down:a crown:n brown:a gown:n",
    "ar": "star:n far:a jar:n car:n guitar:n:2 afar:a:2",
    "eam": "dream:nv stream:n beam:nv gleam:nv seam:n team:n cream:n",
    "ill": "hill:n still:a mill:n thrill:nv spill:v windowsill:n:3",
    "ove": "love:nv dove:n glove:n above:a:2",
    "ake": "lake:n cake:n break:nv wake:v shake:nv bake:v",
    "ong": "song:n long:a strong:a belong:v:2 gong:n",
    "ound": "sound:nv ground:n round:a hound:n bound:a",
    "ine": "line:n vine:n pine:n sign:n shine:v fine:a combine:v:2 divine:a:2 align:v:2",
}


@dataclass(frozen=True)
class EndWord:
    word: str
    pos: str          # "n", "a" or "v"
    syllables: int
    group: str


def _parse_rhymes() -> dict[str, tuple[EndWord, ...]]:
    groups: dict[str, tuple[EndWord, ...]] = {}
    for group, text in _RHYMES.items():
        entries = []
        for item in text.split():
            parts = item.split(":")
            word, flags = parts[0], parts[1]
            syllables = int(parts[2]) if len(parts) == 3 else 1
            for pos in flags:
                entries.append(EndWord(word, pos, syllables, group))
        groups[group] = tuple(entries)
    return groups


RHYME_GROUPS: dict[str, tuple[EndWord, ...]] = _parse_rhymes()
WORLD_BY_ID = {world.id: world for world in WORLDS}

# Words and phrases that sound like a radio host, an announcer or a station. The lexicon uses
# none of them, and the scaffold generator refuses to emit them even if the bank is edited.
DJ_SPEECH_PHRASES = (
    "you're listening", "you are listening", "listening to", "tuned in", "tune in", "stay tuned", "this is",
    "welcome to", "welcome back", "coming up", "up next", "next up", "right after", "after the break",
    "back after", "on the air", "on air", "your host", "your dj", "station id", "call sign", "all request",
    "brought to you", "thanks for listening", "thank you for listening", "listeners", "news at",
    "weather report", "traffic report", "ladies and gentlemen", "good morning", "good evening",
    "good afternoon", "radio", "broadcast", "announcer", "the dj", "a dj", " dj ", "d.j.", "airwaves",
    "frequency", "on the dial", "our sponsor", "sponsored by", "commercial break", "studio",
)
