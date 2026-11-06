"""Grounding lint: find numbers, names and web addresses in a script that the facts do not support.

The composer may state a specific only if the caller's Facts (or the host's own name and phrases)
already contain it. :func:`grounding_lint` checks that after the fact. It is deliberately
conservative: it would rather flag a harmless sentence than let an invented number, date, name or
address reach the air. Anything it flags makes the composer fall back to a plain script.

What counts as a specific:

* numbers written with digits (``7``, ``1,000``, ``3.5``, ``9:07``, ``2026-11-05``, ``21st``) and in
  words (``seven``, ``twenty-three``, ``nineteen ninety-eight``, ``twenty-first``, ``a hundred``,
  ``the nineties``, ``number one``). The ordinary words ``one``, ``first`` and ``second`` are not
  counted on their own ("one of my favourites", "first up", "a second");
* capitalised names, including names made of several words, hyphenated names and shouted words,
  except the first word of a sentence when it is an ordinary English word;
* web addresses, e-mail addresses and spoken forms such as "dot com". Anything shaped like ``name.ending`` counts
  (``radio.ai``, ``city.gov/news``, ``astro.garden``), whatever the ending, when the ending is written in lower case
  or in capitals; the usual endings are also found in mixed case. Abbreviations such as ``e.g.`` and ``a.m.`` are not
  addresses.

Only the first 6,000 characters of a text are examined; a longer text gets one extra warning saying so.
"""
from __future__ import annotations

import functools
import re
import unicodedata
from typing import Any, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

from .textsafe import strip_markup

MAX_FINDINGS = 12

# ---- vocabulary ------------------------------------------------------------------------------------
COMMON_WORDS = frozenset("""
a an the and or but nor so yet for if then than that this these those there here when where while why how what
which who whom whose whether because since until unless although though as at by from in into of on onto off out
over under up down with without within about above across after against along among around before behind below
beneath beside between beyond during except inside near next past through throughout toward towards upon via per
plus like unlike i me my mine myself you your yours yourself yourselves he him his himself she her hers herself it
its itself we us our ours ourselves they them their theirs themselves one ones anyone everyone someone nobody
everybody somebody anybody everything something anything nothing everywhere somewhere anywhere nowhere
is am are was were be been being have has had having do does did doing done will would shall should can could may
might must let lets go goes going gone went get gets got getting give gives gave given make makes made making take
takes took taken taking come comes came coming see sees saw seen seeing look looks looking listen listens listening
hear hears heard hearing play plays played playing start starts started starting stop stops stopped keep keeps kept
keeping stay stays stayed staying hold holds held holding enjoy enjoys enjoyed enjoying thank thanks welcome
welcomes need needs needed want wants wanted love loves loved likes liked know knows knew known think thinks
thought feel feels felt hope hopes hoped help helps try tries tried bring brings brought send sends sent tell tells
told say says said ask asks asked call calls called tune tunes tuned turn turns turned run runs ran running open
opens opened close closes closed end ends ended begin begins began begun find finds found put puts set sets share
shares shared sit sits sat rest rests rested pour pours poured pull pulls grab grabs light lights lit lean leans
settle settles relax relaxes breathe breathes imagine imagines picture pictures remember remembers sounds sound
sounded drift drifts drifted float floats glow glows wander wanders follow follows leave leaves left wait waits
waiting join joins joined meet meets met sing sings sang song songs read reads write writes wrote fix fixes build
builds built mend mends sweep sweeps sharpen measure measures check checks cut cuts wire wires
now soon later today tomorrow yesterday tonight again once twice always never often sometimes usually maybe perhaps
just only also even still already almost quite very really too much more most less least many few several some any
all both each every no not none other another such same different good great nice lovely fine well better best
little big small long short old new young high low late early first last final own whole half hello hi hey
goodbye bye cheers yes yeah okay ok oh ah ahead away back home right soft softly slow slowly quiet quietly easy
easily warm warmly gentle gently bright brightly calm calmly steady steadily clear clearly kind kindly dear
dearly plain plainly simple simply deep deeply wide quick quickly fast slowly sweet sweetly cool cold hot dark
darker lighter golden silver gray grey blue green red yellow orange pink purple brown black white open
radio station music track tracks record records album albums band bands artist artists singer voice voices
airwaves air wave waves dial frequency stream channel studio booth mic microphone host hosts guest guests
listener listeners friend friends neighbour neighbours neighbor neighbors night nights day days morning mornings
afternoon afternoons evening evenings noon midnight hour hours minute minutes moment moments time times week weeks
weekend weekday year years season spring summer autumn fall winter people world place places room rooms window
windows street streets road roads city town garden gardens kitchen coffee tea kettle lamp lamps candle candles
rain snow wind sky star stars moon sun cloud clouds river sea ocean sunrise sunset dawn dusk morning bench
workbench workshop shop tools tool sawdust solder wood metal paper glass table tables chair chairs door doors
thread threads project projects idea ideas plan plans work works worked working job jobs craft crafts maker makers
volunteer volunteers community neighbourhood neighborhood local news update updates item items story stories
source sources report reports reported notes note topic topics brief briefs line lines request requests message
messages sponsor sponsors sponsored advertisement ad ads break breaks program programs show shows set sets
mix mixes playlist playlists tune melody melodies rhythm rhythms beat beats bass piano guitar drum drums horn
horns brass strings keys key
okay alright anyway meanwhile however therefore besides instead otherwise finally eventually suddenly
whatever whenever wherever whoever whichever hmm mm well thanks thank pleasure sorry please
here's there's that's it's what's let's who's how's where's when's we're you're they're we'll you'll they'll
we've you've they've we'd you'd they'd i'm i'll i've i'd isn't aren't wasn't weren't don't doesn't didn't won't
wouldn't can't couldn't shouldn't haven't hasn't hadn't
""".split())


_MORE_WORDS = """
visit try use add allow appear arrive believe belong borrow bounce carry catch change choose clap climb collect
connect consider continue cook count cover create cross dance decide describe discover dream dress drink drive
drop eat enter explore face fill finish fit fly forget forgive gather grow guess hang happen hate hug hum hurry
imagine include invite jump kick kiss knock laugh learn lift listen live lose mark mind miss move name notice
offer order pack paint pass pause pick plan plant point pray press pretend promise protect push reach remind
repeat rise roll rush save search seem sell shake shine shout show sing sleep slip smile spend spin stand step
stir stretch study suppose surprise swing talk taste teach tie touch travel trust try twist visit walk warm wash
watch wave wear welcome wish wonder worry wrap yell
able afraid alive alone amazing ancient awake aware beautiful busy careful certain cheerful clean clever comfortable
common complete cosy cozy crisp curious daily dim distant dry eager easy empty endless entire exact extra fair
familiar famous fancy favourite favorite fierce fresh friendly full funny gentle glad gorgeous grand happy heavy
honest hungry huge hushed important kind lazy light likely lively lonely lost loud lucky mellow mild modern narrow
natural nearby neat nervous odd official pleasant polite possible pretty proud pure rare ready real rich rough
round sad safe sharp shiny shy silent silly sleepy smooth snug soft special strange strong sunny sure tender
thick thin tiny tired tough true ugly useful warm weary weird wild wise wonderful worth young
actually exactly especially honestly lately mostly nearly normally obviously probably properly quickly rarely
recently seriously truly typically ultimately certainly clearly definitely hopefully luckily thankfully
okay oh ooh wow whoa yay phew alas aha ahh eh hm ha mm mmm mhm hmm uh um resume resumes resumed resuming
""".split()
COMMON_WORDS = COMMON_WORDS | frozenset(w for w in _MORE_WORDS)

_EVEN_MORE_WORDS = """
commentary unconfirmed opinion confirmed hands top cue heads identification according attention adjourned adjourn please interrupt interrupting observation
paid solid expect draw sweep carry whisper whispers whispering gently softly quietly slowly quickly happily warmly
absolutely already also although always among another anyone anyway apparently around away barely basically
because before behind besides between beyond briefly certainly clearly completely constantly currently definitely
despite directly during eventually everyone exactly finally frankly fully generally gladly hardly honestly
immediately indeed instead largely lately likewise mainly meanwhile merely mostly naturally nearly neither never
nevertheless nonetheless normally obviously occasionally otherwise overall perhaps personally possibly practically
precisely presumably previously primarily probably promptly quite rarely rather readily really recently regardless
relatively seldom seriously shortly simply slightly somehow somewhat specifically still strangely suddenly supposedly
surely surprisingly thankfully therefore thus tonight totally truly typically ultimately unfortunately usually
virtually whenever whereas wherever whether whichever while whoever whose within without yes yet
announcing attention bulletin broadcast broadcasting chatting chat conversation dedication dedications dedicated
dial dials earphones frequency headphones hosting hosted interlude intermission jingle lineup microphone
playlist programme programming presenter presenting reception request requested requests schedule scheduled
signal sign signing signed speaker speakers static tuning volume
acoustic album anthem ballad beat bridge chorus classic composer concert cover drummer duet ensemble groove harmony
hook improvisation instrumental lyric lyrics melody mixtape note notes opera orchestra performance pianist rhythm
riff rhythm solo soundtrack symphony tempo tracklist verse vocal vocals
afternoon again ahead alone along answer anybody appreciate arrange ask atmosphere attempt aware
back bake bath beautiful begin below bend best bit blanket blend bless blow boil book bottom bread break breath
bright bring broken brush buy cake calm candle capture cheer chill choose clean clear clock coat colour color
comfort company confidence cosy cozy cup curl cushion cut dance daylight decide delight dessert dinner
doorway dream drift drink easy eat embrace empty energy enough entire evening exhale extra fall favourite favorite
feel fill finally fire flavour flavor flicker floor flow flower focus fold forget forgive fresh fridge frost garden
gather gift glad glimmer glow grab grateful grin ground guide habit hang happy harbor harbour heart heat hold
honey hour house hug hum hush idea inhale inside invite jacket jar journey joy keep key kind kitchen knit lamp
laugh lazy leaf leave lend lift light linger listen little loaf lonely look loop loose lovely luck lunch magic
make meal meet melt memory mend mess mind mirror mist moment morning mug nap needle nest night noise nook offer
open pace page pause peace pencil picnic pillow pocket porch pour pretty promise puddle quilt quiet rain rainy
reach rest ribbon ring roast rock roll roof room rug rush saucer scarf scent season seat seed settle shade share
shelf shine shoe shoulder sigh simple sip skip sleep slip slow smell smile smoke snack soak sock soft soup spark
spoon spread spring stairs star steam step stew stir stitch stove stretch sugar sunlight sweater swing take
teapot tender thank thread tidy toast tonic tune umbrella unwind view wait wake walk warm wash watch water wave
weather wind window wish wonder wool worry wrap yarn yawn
"""
COMMON_WORDS = COMMON_WORDS | frozenset(_EVEN_MORE_WORDS.split())

_AND_MORE_WORDS = """
proceed applause silence slide lock plenty buckle crank whew phew oops hooray cheers bravo encore ahem psst shh
admire advise agree announce apologize applaud approach argue arrange ask attend avoid balance beat belong bend bite
blow board borrow bounce breathe bring brush build burn bury call care carry cast celebrate challenge charge chase
cheer chew circle claim clap clear click climb collect combine compare complete concentrate confirm connect consider
continue control convince copy correct cough count crack crash create cross cry curl dare deal declare deliver
demand deny depend deserve destroy develop disappear discuss divide double doubt drag drain draw dress drift drive
drop dry earn educate empty encourage engage ensure escape examine exchange excuse exist expand explain express face
fade fail fear feed fetch fight figure finish fit flash flip float fold force form frame freeze gain gather glance
grab grant greet grind guess hand handle happen hate heal help hide hit hurry hurt imagine improve include increase
insist introduce invite join judge jump kick kill kneel knock label land last laugh lay lead lean learn lend let lie
lift limit link load lock marry matter mean measure melt mention mind miss mix mount move murmur name need nod
notice obey observe obtain occur offer open order pack pass pay perform pick place plan plant plenty point pour
practice prefer prepare present press pretend prevent print promise protect prove provide pull push question quote
raise reach realize receive recognize record reduce refuse relax release remain remove repeat replace reply report
represent rescue respect respond result return reveal ride ring rise roll rub run rush save scatter search select
sense serve settle shake shape share shift shine shoot shout shrug sign sink slow smile snap soften solve sort
sound spare speak speed spell spend spin spread stand stare start state steal steer stick stir stop store strike
struggle study succeed suffer suggest supply support suppose surprise survive suspect swallow swim switch tap taste
tell tend test thank tie tip touch trace train transfer trap travel treat trim trust turn twist unlock unwind
upset use visit wait wake walk wander warn wash waste watch wave wear weigh welcome whisper win wipe wish wonder
work worry wrap write yield
airport alley apartment arm attic baby backyard bag balcony band bank barn basement beach bed bell bike bird
blanket boat bookshelf boots bottle bowl box branch bridge brook brother bucket bus butter button cabin cafe
candy cap car card carpet cart cat ceiling chain chalk cheese child chimney church circle clay closet cloth coin
corner couch counter country cousin cradle crowd curtain dad dawn desk dish dog dollar door dress drum duck dust
ear earth egg engine eye fabric face family farm father feather fence field finger fish flag flame flute fog
forest fork frame friend frog fruit furniture game gate ghost girl glove goat grass guitar hair hall hammer hand
hat hawk hill hole honey horse hotel ice island jar jungle kid kite knee knife ladder lake leg letter library lip
lock lunch map market mat mom monkey mother mountain mouse mouth neck net nose nut ocean office oven owl pad paint
pan park party path pea pen pepper phone piano pie pig pipe plate pond pool pot pup puppy rabbit radio rail rice
river road rock roof root rope sail salt sand school sheep shell ship shirt shop shore sink sister skirt slide
smoke snake soap sock song spider square squirrel stage stone store storm stove street string sugar suit table
tail tent thumb ticket tiger tire toe tool tooth tower town toy train tree truck tub tunnel valley wall wheel
whistle wing wolf wood yard zoo
"""
COMMON_WORDS = COMMON_WORDS | frozenset(_AND_MORE_WORDS.split())

_SUFFIXES = (("ing", ""), ("ing", "e"), ("ed", ""), ("ed", "e"), ("es", ""), ("s", ""), ("ly", ""), ("er", ""),
             ("est", ""), ("ies", "y"), ("ied", "y"))


def _is_common(folded: str) -> bool:
    """An ordinary English word, possibly inflected ('Listening', 'Gently', 'Tries')."""
    if folded in COMMON_WORDS:
        return True
    for suffix, restore in _SUFFIXES:
        if folded.endswith(suffix) and len(folded) - len(suffix) >= 3:
            stem = folded[: -len(suffix)] + restore
            if stem in COMMON_WORDS or (len(stem) > 3 and stem[-1] == stem[-2] and stem[:-1] in COMMON_WORDS):
                return True
    return False


ALLOWED_ACRONYMS = frozenset(("FM", "AM", "PM", "DJ", "ID", "OK", "MC", "TV", "I"))
HONORIFICS = frozenset(("mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "no", "vs", "etc"))
ABBREVIATIONS = frozenset(("no", "mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "vs", "etc", "inc", "ltd", "co",
                           "approx", "dept", "fig", "vol", "ave", "mt"))
I_FORMS = frozenset(("i", "i'm", "i'll", "i've", "i'd"))

_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                     "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 10 ** 6, "billion": 10 ** 9}
_ORD_UNITS = {"third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
              "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15, "sixteenth": 16,
              "seventeenth": 17, "eighteenth": 18, "nineteenth": 19}
_ORD_TENS = {"twentieth": 20, "thirtieth": 30, "fortieth": 40, "fiftieth": 50, "sixtieth": 60, "seventieth": 70,
             "eightieth": 80, "ninetieth": 90}
_ORD_SCALES = {"hundredth": 100, "thousandth": 1000, "millionth": 10 ** 6, "billionth": 10 ** 9}
_TENS_PLURAL = {"twenties": 20, "thirties": 30, "forties": 40, "fifties": 50, "sixties": 60, "seventies": 70,
                "eighties": 80, "nineties": 90}
_FIRST_SECOND = {"first": 1, "second": 2}
_NUMBER_WORDS = frozenset(_UNITS) | frozenset(_TENS) | frozenset(_SCALES) | frozenset(_ORD_UNITS) | frozenset(_ORD_TENS) \
    | frozenset(_ORD_SCALES) | frozenset(_TENS_PLURAL) | frozenset(_FIRST_SECOND) | {"dozen"}
# Words after "one" that make it a count rather than a pronoun.
_ONE_UNITS = frozenset("""hour hours minute minutes day days week weeks month months year years decade decades
century centuries percent o'clock mile miles dollar dollars cent cents pound pounds euro euros hundred thousand
million billion""".split())
_ONE_BEFORE = frozenset(("number", "top", "track", "chapter", "volume", "page", "channel", "episode"))

_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*(?:-[^\W\d_]+(?:['’][^\W\d_]+)*)*")
_DIGITS = re.compile(r"\d{1,3}(?:,\d{3})+(?!\d)|\d+")
# Endings that are found in any mix of upper and lower case. Every other ending is found when it is written
# in lower case or in capitals ("radio.ai", "RADIO.AI"), which keeps "end.Then" (a missing space) from looking like one.
_ENDINGS = ("com|net|org|edu|gov|mil|int|io|ai|fm|co|cc|sh|ly|gg|tv|me|us|uk|de|fr|ca|au|nz|ie|es|nl|se|ch|eu|ru|cn|jp|kr|br|mx|"
            "app|dev|info|biz|xyz|online|site|live|radio|music|garden|cloud|tech")
_SPOKEN_ENDINGS = ("com|net|org|edu|gov|io|ai|fm|co|info|biz|app|dev|xyz|garden|radio|music|online|site|live|tv|sh|ly|gg|cc|"
                   "cloud|tech")
URL = re.compile(
    r"(?i:\bhttps?://|\bwww\.)[^\s<>\"]+"                                    # a scheme or www
    r"|\b[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,5}"                       # an e-mail address
    r"|\b(?:[^\W_][\w-]{0,62}\.){1,6}(?:(?i:" + _ENDINGS + r")|[a-z]{2,24}|[A-Z]{2,24})(?![\w@])(?:/[^\s<>\"]*)?"   # name.ending/path
    r"|(?i:\bdot\s+(?:" + _SPOKEN_ENDINGS + r")\b)")                            # "example dot com"
_URL = URL      # the old private name
LINT_LIMIT = 6000      # characters of a text that are examined
_OPENERS = " \t\"“”‘’'«»(["


class Finding(NamedTuple):
    kind: str      # "number", "name" or "url"
    text: str      # what was found, as it appears in the script
    key: str       # normalised form used for matching


def has_address(text: object) -> bool:
    """True when ``text`` holds a web address, an e-mail address or a spoken one ("example dot com")."""
    return isinstance(text, str) and URL.search(text) is not None


# ---- normalisation helpers -------------------------------------------------------------------------
def _fold(word: str) -> str:
    """Lower-case, accent-free, possessive-free form of a word, used to compare names."""
    w = word.replace("’", "'")
    if w.lower().endswith("'s"):
        w = w[:-2]
    elif w.endswith("'"):
        w = w[:-1]
    w = unicodedata.normalize("NFKD", w)
    return "".join(c for c in w if not unicodedata.combining(c)).lower()


def _digit_key(group: str) -> str:
    ascii_digits = "".join(str(unicodedata.digit(ch, 0)) for ch in group if ch != ",")
    return ascii_digits.lstrip("0") or "0"


def _url_key(url: str) -> str:
    u = url.strip().rstrip(".,;:!?)\"'").lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = re.sub(r"\s+", " ", u)
    return u.rstrip("/")


def _domain(url_key: str) -> str:
    return url_key.split("/", 1)[0]


# ---- numbers in words ------------------------------------------------------------------------------
class _Comp(NamedTuple):
    word: str
    start: int
    end: int


def _components(text: str) -> List[_Comp]:
    out: List[_Comp] = []
    for match in _WORD.finditer(text):
        word = match.group()
        position = match.start()
        for part in word.split("-"):
            low = part.lower()
            out.append(_Comp(low, position, position + len(part)))
            position += len(part) + 1
    return out


def _kind(word: str) -> Optional[str]:
    if word in _UNITS:
        return "u"
    if word in _TENS:
        return "t"
    if word in _SCALES:
        return "s"
    if word in _ORD_UNITS:
        return "ou"
    if word in _ORD_TENS:
        return "ot"
    if word in _ORD_SCALES:
        return "os"
    if word in _FIRST_SECOND:
        return "fs"
    if word in _TENS_PLURAL:
        return "tp"
    if word == "dozen":
        return "d"
    if word == "and":
        return "and"
    return None


def _value(word: str) -> int:
    for table in (_UNITS, _TENS, _SCALES, _ORD_UNITS, _ORD_TENS, _ORD_SCALES, _TENS_PLURAL, _FIRST_SECOND):
        if word in table:
            return table[word]
    return 12 if word == "dozen" else 0


def number_words_in(text: str) -> List[Tuple[str, str]]:
    """Numbers written in words: a list of (decimal string, the words as written)."""
    comps = _components(text)
    results: List[Tuple[str, str]] = []
    i, n = 0, len(comps)
    while i < n:
        kind = _kind(comps[i].word)
        if kind is None or kind == "and":
            i += 1
            continue
        # collect a run of adjacent number words
        j = i
        run = [comps[i]]
        while j + 1 < n:
            gap = text[comps[j].end:comps[j + 1].start]
            nxt = _kind(comps[j + 1].word)
            if nxt is None or gap not in (" ", "-"):
                break
            if nxt == "and":
                after = _kind(comps[j + 2].word) if j + 2 < n else None
                if after not in ("u", "t") or text[comps[j + 1].end:comps[j + 2].start] != " " or _kind(comps[j].word) != "s":
                    break
                run.append(comps[j + 1])
                j += 1
                continue
            run.append(comps[j + 1])
            j += 1
        for value, words in _evaluate(run, text):
            results.append((str(value), words))
        i = j + 1
    return results


def _evaluate(run: Sequence[_Comp], text: str) -> List[Tuple[int, str]]:
    """The numbers in a run of adjacent number words, as (value, words)."""
    total = current = 0
    started = False
    words: List[str] = []
    out = []

    def flush():
        nonlocal total, current, started, words
        if started:
            out.append((total + current, " ".join(words)))
        total = current = 0
        started = False
        words = []

    def soft_one_counts() -> bool:
        if len(run) > 1:
            return True
        start, end = run[0].start, run[0].end
        before = text[max(0, start - 24):start].rstrip(" #")
        words_before = before.lower().split()
        if text[max(0, start - 1):start] == "#" or (words_before and words_before[-1].strip(".,;:!?\"'") in _ONE_BEFORE):
            return True
        after = text[end:end + 24].lstrip().lower().split()
        return bool(after) and after[0].strip(".,;:!?\"'") in _ONE_UNITS

    for comp in run:
        kind, value = _kind(comp.word), _value(comp.word)
        if kind == "and":
            words.append(comp.word)
            continue
        if comp.word == "one" and not soft_one_counts():
            flush()
            continue
        if kind in ("tp", "d"):
            flush()
            out.append((value, comp.word))
            continue
        if kind == "fs":
            if started and current % 100 >= 20 and current % 10 == 0:
                current += value
                words.append(comp.word)
            else:
                flush()
            continue
        if kind in ("u", "ou"):
            if value == 0:
                flush()
                out.append((0, comp.word))
                continue
            tail = current % 100
            fits = tail == 0 or (value < 10 and tail >= 20 and current % 10 == 0)
            if started and not fits:
                flush()
            current += value
            started = True
            words.append(comp.word)
        elif kind in ("t", "ot"):
            if started and current % 100 != 0:
                flush()
            current += value
            started = True
            words.append(comp.word)
        else:  # scales
            if value == 100:
                if not started or current == 0:
                    current, started = 100, True
                elif current < 100:
                    current *= 100
                else:
                    flush()
                    current, started = 100, True
            else:
                total += (current or 1) * value
                current = 0
                started = True
            words.append(comp.word)
    flush()
    return out


# ---- extraction ------------------------------------------------------------------------------------
def numbers_in(text: str) -> List[Tuple[str, str]]:
    """Every number in ``text`` as (decimal string, as written), digits first then words."""
    found = [(_digit_key(m.group()), m.group()) for m in _DIGITS.finditer(text)]
    found.extend(number_words_in(text))
    return found


def _year_halves(keys: Iterable[str]) -> Set[str]:
    out: Set[str] = set()
    for key in keys:
        if len(key) == 4 and key[0] in "12":
            out.add(key[:2])
            out.add(key[2:].lstrip("0") or "0")
    return out


class Grounding:
    """What a script is allowed to say: the words, numbers and web addresses found in the facts and
    in the host's own phrases."""

    def __init__(self, texts: Iterable[str], extra_numbers: Iterable[str] = ()):
        self.words: Set[str] = set()
        self.numbers: Set[str] = set(extra_numbers)
        self.urls: Set[str] = set()
        for text in texts:
            if not isinstance(text, str):
                continue
            text = unicodedata.normalize("NFKC", text[:2000])
            for match in URL.finditer(text):
                key = _url_key(match.group())
                self.urls.add(key)
                self.urls.add(_domain(key))
            for word in _WORD.findall(text):
                folded = _fold(word)
                self.words.add(folded)
                if "-" in folded:
                    self.words.update(part for part in folded.split("-") if part)
            for key, _ in numbers_in(text):
                self.numbers.add(key)
        self.numbers |= _year_halves(set(self.numbers))

    def has_word(self, folded: str) -> bool:
        if folded in self.words:
            return True
        return "-" in folded and all(part in self.words for part in folded.split("-") if part)


def _starts_sentence(text: str, start: int, lenient: bool = False) -> bool:
    """True when the word at ``start`` opens a sentence. With ``lenient`` a word that follows an
    abbreviation such as "Co." also counts, because it often does ("Corner Bike Co. Please call.")."""
    i = start - 1
    quoted = False
    while i >= 0 and text[i] in _OPENERS:
        quoted = quoted or text[i] in "\"\u201c\u201d\u2018\u00ab"
        i -= 1
    if i < 0:
        return True
    ch = text[i]
    if ch == "\n" or ch in "!?\u2026":
        return True
    if ch == ".":
        before = re.search(r"([^\W\d_]+)\.$", text[max(0, i - 12):i + 1])
        return lenient or not (before and before.group(1).lower() in ABBREVIATIONS)
    return ch == ":" or (quoted and ch in ",;")


def _examined(text: object) -> "Tuple[str, bool]":
    """The part of ``text`` that is checked (speech markers removed, look-alikes folded to plain characters, cut to
    ``LINT_LIMIT``), and whether anything was left unchecked."""
    if not isinstance(text, str):
        return "", False
    normalized = unicodedata.normalize("NFKC", strip_markup(text[:20000]))   # so fractions, circled and full-width forms are seen
    return normalized[:LINT_LIMIT], len(text) > 20000 or len(normalized) > LINT_LIMIT


def find_ungrounded(text: str, grounding: Grounding) -> List[Finding]:
    """Everything specific in ``text`` that ``grounding`` does not support, in reading order."""
    return _scan(_examined(text)[0], grounding)


def _scan(text: str, grounding: Grounding) -> List[Finding]:
    findings: List[Finding] = []
    # web addresses first, and blank them out so their pieces are not counted again
    scrubbed = text
    for match in URL.finditer(text):
        shown = match.group().rstrip(".,;:!?)\"'")
        key = _url_key(shown)
        if key not in grounding.urls and _domain(key) not in grounding.urls:
            findings.append(Finding("url", shown, key))
        stop = match.start() + len(shown)   # the full stop that ends a sentence stays, so the next word still opens one
        scrubbed = scrubbed[:match.start()] + " " * (stop - match.start()) + scrubbed[stop:]
    for key, shown in numbers_in(scrubbed):
        if key not in grounding.numbers:
            findings.append(Finding("number", shown, key))
    run: List[str] = []
    run_bad: List[str] = []

    def close_run():
        if run_bad:
            findings.append(Finding("name", " ".join(run_bad), "|".join(run_bad)))
        run.clear()
        run_bad.clear()

    previous_end = -1
    for match in _WORD.finditer(scrubbed):
        word = match.group()
        capital = word[0].isupper()
        adjacent = previous_end >= 0 and scrubbed[previous_end:match.start()] == " "
        previous_end = match.end()
        if not capital:
            close_run()
            continue
        folded = _fold(word)
        if word in I_FORMS or word.lower() in I_FORMS or folded in HONORIFICS or folded in _NUMBER_WORDS:
            close_run()   # numbers written in words are checked as numbers, not as names
            continue
        if word.isupper() and len(word) > 1 and word in ALLOWED_ACRONYMS:
            close_run()
            continue
        if not adjacent:
            close_run()
        run.append(word)
        if grounding.has_word(folded):
            continue
        if (not (word.isupper() and len(word) > 1) and _is_common(folded)
                and _starts_sentence(scrubbed, match.start(), lenient=True)):
            continue
        run_bad.append(word)
    close_run()
    return findings


def describe(finding: Finding) -> str:
    if finding.kind == "number":
        return f"The number “{finding.text}” is not in your facts."
    if finding.kind == "url":
        return f"The web address “{finding.text}” is not in your facts."
    return f"The name “{finding.text}” is not in your facts."


@functools.lru_cache(maxsize=256)
def _grounding(texts: Tuple[str, ...], numbers: Tuple[str, ...]) -> "Grounding":
    return Grounding(texts, numbers)


def grounding_texts(facts: Any, persona: Any = None, extra: Iterable[str] = ()) -> Tuple[List[str], List[str]]:
    """Gather the texts and numbers that ground a script."""
    from .facts import Facts

    texts: List[str] = []
    numbers: List[str] = []
    if facts is None:
        pass
    elif isinstance(facts, Facts):
        texts.extend(facts.texts())
        numbers.extend(facts.extra_numbers())
    elif isinstance(facts, Mapping):
        cleaned, _ = Facts.from_any(facts)
        texts.extend(cleaned.texts())
        numbers.extend(cleaned.extra_numbers())
    elif isinstance(facts, (list, tuple, set, frozenset)):
        texts.extend(x for x in facts if isinstance(x, str))
    elif isinstance(facts, str):
        texts.append(facts)
    if persona is not None:
        gather = getattr(persona, "grounding_texts", None)
        texts.extend(gather() if callable(gather) else ())
    texts.extend(x for x in extra if isinstance(x, str))
    return texts, numbers


def grounding_lint(text: str, facts: Any, persona: Any = None, *, extra: Iterable[str] = ()) -> List[str]:
    """Plain-language warnings for every number, capitalised name or web address in ``text`` that
    is not in ``facts`` (or in the host's own name and phrases). An empty list means every
    specific in the text is backed by the facts.

    ``facts`` may be a :class:`Facts`, a mapping of raw facts, or a list of strings. Speaker
    markers and event tags in ``text`` are ignored.
    """
    texts, numbers = grounding_texts(facts, persona, extra)
    examined, cut = _examined(text)
    findings = _scan(examined, _grounding(tuple(texts), tuple(numbers)))
    seen: Set[Tuple[str, str]] = set()
    warnings: List[str] = []
    room = MAX_FINDINGS - 1 if cut else MAX_FINDINGS      # the note about the unchecked part counts towards the limit
    for finding in findings:
        marker = (finding.kind, finding.text.lower())
        if marker in seen:
            continue
        seen.add(marker)
        warnings.append(describe(finding))
        if len(warnings) >= room:
            break
    if cut:
        warnings.append(f"This text is longer than the {LINT_LIMIT:,} characters that can be checked, so the rest was not looked at.")
    return warnings
