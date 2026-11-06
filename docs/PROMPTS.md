# Word-salad music library

## Purpose

Turn a user's station style into a rich, reproducible **music brief** for YuE2. This is not a DJ-script generator and does not send work to Drama 3.

KnightshiftRadio's existing generator uses compatible instruments, mood, harmony, groove, and form rather than indiscriminate genre combinations. This implementation keeps that principle while making compositional development and musical complexity explicit. It is a new implementation, not a copy of the live station's generator.

Complexity here means **harmony, rhythm, motifs, arrangement development and instrument roles**. It never means arbitrary genre stacking, and it never means loudness.

## What ships

The versioned JSON catalog now contains **71 authored style palettes** and **18 authored arrangement designs**. Their cross-product gives **1,278 stable preset identities**. Each palette and arrangement carries separate simple, layered, and intricate musical instructions; the compiler does not create complexity merely by appending the word "complex."

| Catalog version | Added | Totals |
| --- | --- | --- |
| `1.0.0` | 32 style palettes, 12 arrangement designs | 384 presets |
| `1.1.0` | 39 style palettes, 6 arrangement designs (every palette meets every design) | 71 palettes, 18 designs, 1,278 presets |

This is deliberately transparent about scale: 1,278 *composed* presets, not 1,278 independently hand-written compositions. Three complexity levels, three densities, and two modes yield **23,004** compiler combinations (1,278 x 3 x 3 x 2); the test suite compiles every one. Those combinations are structural checks, **not a musical-quality metric**.

### The original 384 presets are frozen

The 384 presets of catalog `1.0.0` keep their ids and their composed text exactly. The original two data files are untouched, new content lives in its own files (`palettes-1.1.0.json`, `arrangements-1.1.0.json`), and a preset's randomness is keyed by the catalog version in which its palette and arrangement were introduced, so adding styles can never reshuffle an old preset. `tests/test_prompts_golden.py` freezes the id list and a SHA-256 of each old preset's composed text (36 compiled variants per preset) and was generated from the untouched catalog before any change. `load_library(through="1.0.0")` (or `--catalog 1.0.0` on the command line) returns the original 32 x 12 library for replaying old records.

What can change: `Library.choose()` draws from whatever presets match the filters, so a seed picks differently now that there are more candidates. A stored record names its preset id, so replay it with `generate()` or `replay()`, not by choosing again.

### Style palettes

The first 32 palettes (catalog `1.0.0`):

| Palette ID | Identity |
| --- | --- |
| `warm-brush-jazz` | Soft swing, rich voice leading, relaxed acoustic conversation |
| `modal-night-jazz` | Modal development, quartal voicings, long pedal tones |
| `bossa-courtyard` | Nylon strings, interlocking bossa rhythm, warm chromatic movement |
| `afro-cuban-session` | Consistent 2-3 son clave, montuno, tumbao, ensemble answers |
| `ethiopian-jazz-evening` | Pentatonic/modal figures and a rolling six-beat cycle |
| `jazz-waltz` | Three-beat lyricism, soft cross-rhythms, flowing inner voices |
| `chamber-jazz` | Small acoustic ensemble, breathing counterpoint |
| `soul-jazz-organ` | Organ, gospel movement, grounded groove |
| `neo-soul` | Relaxed pocket, extended harmony, expressive bass |
| `pocket-funk` | Interlocking sixteenths and purposeful rests |
| `deep-house` | Stable dance pulse, warm chord development |
| `broken-beat` | Syncopated drum cells, quartal colors, coordinated returns |
| `liquid-drum-and-bass` | Fast rolling breaks under spacious melodic writing |
| `dub-techno` | Sparse chord echoes, evolving inner voices, bounded feedback |
| `ambient-garden` | Slow harmonic change and quiet interleaving figures |
| `synthwave-horizon` | Tuneful analog/digital hooks and structured harmonic returns |
| `downtempo-trip-hop` | Slow broken drums, reflective melody, warm resolutions |
| `progressive-electronica` | Sequenced development rather than endless filter builds |
| `chamber-minimalism` | Small cells, staggered entries, gradual harmonic change |
| `impressionist-piano` | Six-beat motion, color harmony, controlled pedal resonance |
| `cinematic-orchestra` | Thematic development, clear orchestration, earned resolution |
| `string-quartet` | Four-part conversation, imitation, suspensions |
| `progressive-rock` | 7/8 grouped 2+2+3, thematic instrumental development |
| `post-rock` | Interlocking guitar lines, contrast beyond loud crescendos |
| `math-rock` | 5/4 grouped 3+2, coordinated rests, clean tapped figures |
| `psychedelic-folk` | Modal fingerpicking, drones, six-beat sway |
| `bluegrass-trail` | Acoustic interplay, rolling accents, recognizable refrains |
| `highlife-guitar` | Complementary guitar figures and melodic bass |
| `desert-guitar` | Modal repetition, expressive bends, six-beat interplay |
| `balkan-chamber` | 7/8 grouped 3+2+2, ornamented ensemble dialogue |
| `dub-reggae` | One-drop rhythm, bass-led space, deliberate echo answers |
| `tango-nuevo` | Dramatic pauses, 3+3+2 accents, chromatic counterlines |

The 39 added in `1.1.0`. Entries marked (H) carry the tag `heritage-inspired` and the catalog shows them a plain note (see below):

| Palette ID | Identity |
| --- | --- |
| `kora-river-ostinato` (H) | Kora and balafon ostinato cycles in twelve pulses grouped in threes |
| `afrobeat-long-vamp` (H) | Interlocking guitars and a horn riff over a long minor-seventh vamp |
| `amapiano-log-drum` (H) | Jazzy piano chords over a log-drum bass at a relaxed dance tempo |
| `maqam-courtyard-ensemble` (H) | Oud, ney, qanun and hand drums in a maqam mode; no fixed-pitch keyboards |
| `makam-aksak-evening` (H) | Clarinet, saz and kanun over a nine-pulse 2+2+2+3 cycle |
| `raga-inspired-evening` (H) | Sitar, bansuri and tabla over a drone in a ten-beat 2+3+2+3 cycle |
| `carnatic-inspired-trio` (H) | Violin or flute with mridangam and ghatam over a drone in an eight-beat 4+2+2 cycle |
| `japanese-garden-ensemble` (H) | Shakuhachi or shamisen with koto, a bass koto and sparing taiko |
| `guzheng-erhu-river` (H) | Erhu or dizi with guzheng and pipa in pentatonic phrases |
| `gamelan-slendro-shimmer` (H) | Tuned metal, interlocking bonang and a large gong marking a long cycle |
| `sunset-city-pop` (H) | Glossy major-seventh loops with electric piano, saxophone, funky guitar and a key lift |
| `celtic-session-reels` (H) | Fiddle, wooden flute, bodhran and bouzouki in a modal reel |
| `slip-jig-parlour` (H) | A lilting nine-pulse slip jig for fiddle, concertina and piano |
| `nordic-polska-fiddle` (H) | Fiddle or nyckelharpa over a drone in a leaning three-beat dance |
| `flamenco-compas-guitar` (H) | Guitar, handclaps and cajon on a twelve-count compás with a Phrygian cadence |
| `klezmer-freylekh-dance` (H) | Clarinet and violin ornaments in the freygish mode over an oom-pah pulse |
| `kopanitsa-village-dance` (H) | Bagpipe, gadulka and tapan in an eleven-pulse 2+2+3+2+2 dance |
| `musette-waltz-accordion` (H) | Accordion and clarinet waltz with a cafe feel |
| `baroque-trio-sonata` | Two upper voices and harpsichord continuo with imitation and sequences |
| `old-time-clawhammer-porch` (H) | Fiddle, clawhammer banjo and dulcimer drone in a steady two-beat bounce |
| `porch-blues-slide` | Slide guitar and harmonica over a slow twelve-bar shuffle |
| `ragtime-parlour-piano` (H) | Syncopated upright piano with banjo and clarinet in a two-beat oom-pah |
| `second-line-brass-parade` (H) | Trumpet, trombone, clarinet and sousaphone in a syncopated parade groove |
| `steel-guitar-waltz` | Pedal steel and fiddle over a gentle country waltz |
| `string-swing-cafe` | Lead guitar and violin over a driving acoustic guitar pump |
| `hard-bop-front-line` | Trumpet and tenor saxophone front line with piano trio swing |
| `andean-huayno-altiplano` (H) | Quena, panpipes in interlocking pairs, charango and bombo in a galloping two-beat |
| `cumbia-accordion-slow` (H) | Accordion and gaita flute over scraper and hand drums |
| `son-jarocho-sesquialtera` (H) | Requinto, harp, jarana and footwork alternating 6/8 and 3/4 |
| `choro-roda-afternoon` (H) | Flute and clarinet with cavaquinho, pandeiro and a seven-string guitar bass |
| `steel-pan-calypso` (H) | Steelpan melody over rhythm guitar upstrokes and a bouncing bass |
| `charanga-danzon-salon` (H) | Flute and violins over piano and a courtly two-beat pulse |
| `lofi-study-loops` | Mellow electric piano over dusty drums, vinyl texture and warm sub-bass |
| `motorik-highway` | Chiming guitar and sequencer over an unbroken four-beat motorik pulse |
| `dream-pop-haze` | Chorused guitar, soft synthesizer and a hazy reverb wash at low dynamics |
| `two-step-garage` | Chopped electric piano chords and sub-bass over a skipping two-step pattern |
| `chiptune-quest` | Pulse-wave leads, arpeggios and triangle bass, four voices at a time |
| `celesta-lullaby` | Celesta and music box over harp and cello in a rocking six-beat lullaby |
| `phasing-mallets` | Marimba and vibraphone patterns that drift out of unison |

**Named traditions are style directions, not claims of authenticity or cultural authority.** The new texts were written from published musical features (modes, rhythmic cycles, instrument roles). They name no living artist, avoid wording such as "exotic" or "authentic", and have **not been reviewed by practitioners of these traditions**. Listening and expert or community feedback must guide revisions. The catalog API gives every heritage style the note: *"A style direction inspired by this tradition, written from its published musical features. It is not a claim of authenticity, and it cannot replace listening to the real thing."* (The eight older traditional styles `afro-cuban-session`, `ethiopian-jazz-evening`, `bossa-courtyard`, `highlife-guitar`, `desert-guitar`, `balkan-chamber`, `dub-reggae` and `tango-nuevo` get the same note.)

### Arrangement designs

The first twelve: `theme-and-return`, `call-and-response`, `gradual-bloom`, `theme-and-variations`, `interlocking-lines`, `rondo-path`, `arch-form`, `pedal-and-release`, `register-relay`, `contrast-bridge`, `evolving-cycle`, and `miniature-journey`.

Added in `1.1.0`: `ground-and-variations` (a repeating foundation with changing upper variations), `verse-and-refrain` (a song shape that also suits sung music), `shrinking-exchanges` (call and answer that tighten from four bars to one), `canon-and-release` (imitation that overlaps more tightly, then releases), `expanding-phrases` (phrases that lengthen then contract), and `slow-tide` (two slow waves shaped by register and voices, not volume).

Designs control three to six musical sections and how a motif develops between them. They do not inject a second tempo, meter, or unrelated instrumentation: a rule checks that (`arrangement.instrument`, `arrangement.tempo-meter`). The palette remains the authority for the musical vocabulary; this is why combinations are more coherent than random adjective piles.

### Complexity versus density

- **Simple:** recognizable tune, clear form, restrained harmonic/rhythmic variation.
- **Layered:** voice leading, answering parts, distinct middle passages, meaningful variation.
- **Intricate:** imitative or contrary-motion writing, reharmonized returns, rhythmic displacement, motif augmentation/diminution, or common-tone changes appropriate to the palette.

Separately select **sparse**, **balanced**, or **lush** texture. Increasing complexity does not change a preset's energy class or tempo range. The same seed keeps tempo and selected instrument roles fixed when comparing complexity levels, helping eventual listening tests isolate the difference.

## Compatibility rules

Every palette and arrangement is checked by `myradio.prompts.compat` (and `python3 -m myradio.prompts check`). The same rules protect the shipped catalog and every style a person saves. A rule is an **error** (the style cannot be saved or shipped) or a **warning** (worth a look). The rules read text and a small instrument lexicon (`instruments.py`); they find authoring mistakes such as a lead that cannot play a tune, a piano promised with a microtonal scale, or a meter that does not add up. **Passing them does not mean a model plays the result well.**

| Rule | Severity | What it guarantees |
| --- | --- | --- |
| `meter.format` | error | The meter is written like 4/4, 6/8 or 7/8 (2+2+3). |
| `meter.grouping-sum` | error | The groups in brackets add up to the bar. |
| `meter.mention` | error | Any other time signature in the text has the same bar length and is described as a cross-rhythm. |
| `meter.beat-count` | error | An 'N-beat' claim fits the meter, or is described as a cross-rhythm. |
| `meter.grouping-mention` | error | A grouping such as 3+2+2 in the text adds up to the bar and, when the style declares a grouping, rearranges it. |
| `meter.tag` | error | A style tagged or named a waltz is in three, and an 'odd-meter' style has an uneven meter. |
| `tempo.range` | error | The tempo range is ordered, between 30 and 240, and no wider than 40. |
| `tempo.number` | error | A BPM figure in the text lies inside the range (or is marked half-time or double-time). |
| `tempo.word` | error | A 'slow' or 'fast' tempo word agrees with the range. |
| `tempo.energy` | error | The energy class fits the tempo range (calm tops out at 120, steady stays within 60 to 140, driving reaches 96). |
| `instrument.lead-melodic` | error | Every lead can carry a melody. |
| `instrument.bass-slot` | error | The bass part names a bass instrument or a bass role. |
| `instrument.bass-only-elsewhere` | error | Bass-only instruments are not used as a lead or supporting part. |
| `instrument.tradition-mix` | error | Instruments tied to different traditions are mixed only in a style tagged 'fusion'. |
| `instrument.purity` | error | 'acoustic' styles use no electric or electronic instrument; 'chiptune' and 'synthetic' styles use no acoustic one. |
| `instrument.microtonal` | error | Fixed-pitch instruments (piano, accordion, marimba and similar) are not combined with a microtonal scale. |
| `instrument.negation` | error | The text does not rule out an instrument the style names. |
| `instrument.stacking` | error | No more than 3 instruments in one part and 6 playing together. |
| `instrument.duplicate` | warning | The same instrument as lead and support is called a 'second' or 'another' one, so two parts are not played by one. |
| `instrument.unrecognized` | warning | A part names no instrument the checker recognises, so it could not be checked. |
| `energy.intense-instrument` | error | A calm style names no loud or aggressive sound. |
| `energy.loud-claim` | error | A calm style does not describe itself as aggressive, crushing or explosive. |
| `complexity.loudness` | error | Complexity and development are never described as getting louder. |
| `text.speech-markup` | error | No speech tags, announcer or DJ wording in music text. |
| `text.punctuation` | warning | Sentence fields end with a full stop; short phrases do not. |
| `arrangement.instrument` | error | An arrangement names no instrument (it describes roles and shapes only). |
| `arrangement.tempo-meter` | error | An arrangement names no tempo or time signature. |

Details worth knowing:

- A **cross-rhythm** is allowed when the sentence says so ("6/8 against 3/4", "a three-beat phrase over the four-beat bar") and any other time signature has the same bar length.
- A **grouping** may be counted in the bar's own beats or in eighth notes (4/4 may use 3+3+2). A style that declares a grouping may show it rearranged (2+2+3 as 3+2+2) when the text says "rotate", "shift" or similar.
- A **meter** always has at least one beat in the bar and is written in plain digits (`4/4`, `7/8 (2+2+3)`): `0/4`, `00/8`, a leading zero and digits from other scripts are refused by `meter.format`, and so is a zero inside a grouping.
- **Announcer wording** (`text.speech-markup`) covers speech tags and the words DJ and announcer, and also the phrases that belong to a host: "welcome back", "you're listening", "stay tuned", "listeners", "good evening", "thanks for listening", "brought to you by" and similar (the list is the lyric guard's own, leaving out words that have an everyday musical sense: "studio", "radio", "frequency", "tune in", "this is", "coming up"). It reads the style's name and every part of it, and the arrangement's too.
- Text that says what is *not* wanted ("no drum kit", "rather than loudness") is not read as a claim. "Without crowding the guitar" is about an action, not about leaving the guitar out.
- **Tradition-bound instruments** are those tied to one tradition (sitar, koto, gamelan metallophones, bodhran, charango and so on). Widely shared instruments (guitar, violin, flute, congas, accordion) belong to none.
- The catalog's seven original warnings (`instrument.duplicate` on `soul-jazz-organ`, `neo-soul`, `broken-beat`, `synthwave-horizon`, `impressionist-piano`, `string-quartet`, `desert-guitar`, where one instrument serves as both lead and support) are recorded and left alone because those presets are frozen. The 39 new palettes and 6 new designs produce no findings of any severity.

## Sliders

The studio's Sound step has three sliders, each a whole number from 0 to 100. `filters_from_sliders(energy, adventurousness, vocals)` maps them to the library's own filters. The mapping is this plain table.

| Energy slider | Energy class | Texture (density) |
| --- | --- | --- |
| 0 to 33 | calm | sparse |
| 34 to 66 | steady | balanced |
| 67 to 100 | driving | lush |

| How adventurous | Complexity | Styles not repeated | Designs not repeated | Presets not repeated |
| --- | --- | --- | --- | --- |
| 0 to 33 | simple | 2 | 1 | 100 |
| 34 to 66 | layered | 6 | 3 | 200 |
| 67 to 100 | intricate | 12 | 5 | 400 |

**Vocals** is a share, not a switch: 0 means never sung, 100 means always sung, and between those each song is sung with that percentage chance, decided from the seed. The same seed always gives the same answer, and raising the slider only ever turns instrumentals into songs.

**Complex never means loud.** Complexity comes only from the adventurousness slider. The energy class and the texture come only from the energy slider. Moving one slider never changes what the other chose (a test checks all 10,201 energy and adventurousness pairs), and "lush" means fuller registers within the named ensemble, not volume. An intricate calm style keeps the same tempo range and meter as a simple one. The energy class filters styles; it is the style's own label and is not changed by anything else.

Naming an exact preset keeps the preset: the choosing settings (the energy class among them) are ignored, `treatment.energy` reports **the preset's own energy**, and the slider summary adds "You named a calm style, so its own energy was kept." when the slider asked for something else. The texture (density) and complexity settings still apply.

Sliders are **preferences**. In `compose`, explicit `energy`, `complexity`, `density` and `mode` settings override them, and if the slider's energy class would leave no styles among the ones you chose, the energy preference alone is dropped and the result says `relaxed: "slider-energy"`. Explicit filters are never relaxed.

## Lyric scaffold (sung music)

`lyrics_scaffold(preset, seed)` writes a **suggested outline** of lyrics for vocal mode. It is generated text from a small word bank of gentle concrete images, always labelled `source: "scaffold"`, and kept completely separate from supplied lyrics (which are the operator's own words and are passed through untouched and unchanged). It never contains DJ, announcer or station wording and never contains speech tags such as `<|speaker:1|>` or `[laughs]`; a guard rejects both, and tests run it over the whole catalog.

What it produces:

- Section tags on their own lines, the way the reference station's lyric files do (`[Verse 1]`, `[Chorus]`, `[Verse 2]`, `[Chorus]`, `[Outro]`, with `[Bridge]` and a third chorus in the full form). `tag_style="lower"` writes `[verse]`, `[chorus]` instead. **The tag spelling follows the reference lyric files; whether YuE2 prefers one spelling is unverified here.**
- A **syllable plan**: syllables per line follow the style's meter (4/4 gives 8, 3/4 and 6/8 give 6, 7/8 gives 7, 12/8 gives 6, 5/4 gives 10). A meter that cannot be read, or that has no beats in the bar, is refused instead of guessed at. Every **rhymed** line has exactly that many syllables by the bank's own counts, cross-checked in tests by an independent counter. The **recurring hook keeps its own length** (6 or 7 syllables, reported as `motif.syllables`), so in a style whose lines are 8 or 10 syllables long the chorus alternates the hook with a longer line; where the two are equal, as in the 12/8 example below, every line has the same length. These are English singing syllables, not a promise about how the model will time them.
- A **rhyme plan** per section (`ABAB`, `AABB`, `ABCB` for verses; `HAHA` for the chorus; `ABAH` for the bridge; `HA` for the outro). Lines with the same letter rhyme, different letters do not, and no end word is used twice except the hook's.
- A **recurring motif**: one short hook phrase that returns in every chorus, ends the bridge and opens the outro. The chorus is written once and repeated identically.

Example (`porch-blues-slide/verse-and-refrain`, seed 7; real output, uneven poetry and all):

```
[Verse 1]
You shelter a soft kite
Lead the bell by the light
The sail sings the wave strong
You swing a tender song

[Chorus]
The harbor hums a tune
We shimmer past the noon
The harbor hums a tune
Let the umbrella croon
```

It is an outline for a person or an agent to edit, **not finished lyrics and not good poetry**. Edit it, replace it, or supply your own lyrics.

## Your own styles, favourites and repeat memory

These live in the extension's own SQLite file (`<data>/ext/prompts/prompts.sqlite3`, mode 0600). Nothing is written to the station database.

**Saved styles** use the `user:` namespace (`user:evening-porch`), with an id of lowercase letters, numbers and hyphens (up to 63 characters). Three ways to make one, all with the same checks as the catalog (the same JSON schema, then every compatibility rule above):

```json
{"id": "evening-porch", "title": "Evening porch",
 "based_on": "porch-blues-slide/slow-tide",
 "palette": {"name": "Evening Porch Blues", "bpm": [60, 80]}}
```

- **A copy or an edit**: give `based_on` (a built-in or one of your own). `palette` and `arrangement` hold only what you change.
- **A fully written style**: give a complete `palette` object in the catalog's schema, and an `arrangement` that is either a built-in design id (`"canon-and-release"`) or a complete design object.
- A copy is stored **in full**, never as a reference, so a later catalog release cannot change what it composes. Its random choices (tempo, mood, lead, support and motif) are keyed by a digest of its content, its id and the seed, and **never by the catalog version**. The same style and seed give the same choices after a restart, a delete and re-save, on another machine, or after the catalog has grown. (The record still names the catalog version that wrote it, so its fingerprint differs between releases while the words and tempo do not.) Any real edit gives a new digest and a new `revision`.
- A copy of a style based on a tradition keeps the standing note that it is a style direction, however many times it is copied and whatever its tags say now. The lineage is followed back to the built-in style; removing a saved style from the middle of a lineage re-attaches the styles copied from it to its own parent.

What is checked, all of it before anything is saved:

- **The schema and every compatibility rule** above, for the style and for its arrangement design.
- **One tidy line per field.** Every text field, and the title, is a single line with single spaces and nothing at either end, as in the shipped catalog. Line breaks, tabs, doubled spaces, control characters (such as an escape character) and text-direction overrides are refused, so a field can never add a line to a written-out prompt.
- **Whole characters only.** Half an emoji (what is left when a text box cuts one in two) is refused with a plain message, in saved styles, in `compose` requests and in lyrics.
- **No announcer wording** (see the rules above), in the style, its parts and its title.
- **It can be written out.** A seed picks one mood, lead, support and motif and a tempo, so the longest version a style can ever have uses the longest of each. That worst case, for every complexity, density and mode, must fit the 6,000-character limit on a written-out prompt, or the style is refused with the numbers (for example: "This style is too long for the music maker. Written out in full, its longest version is 7,412 characters and the limit is 6,000. Shorten the harmony, rhythm, production, mood or arrangement notes by about 1,412 characters, then save it again."). This is the limit that binds: the 24,000-character document cap is far looser, and the longest any shipped preset can be written out is 2,281 characters. A saved style therefore cannot be too long to write out for one seed and fine for another.

Problems are reported in plain language, say where they are, and say a sentence once when it applies in several places: "This style cannot be saved yet. 1. In the intricate harmony, the layered harmony, the simple harmony and 3 more places: This text makes the music more complex by making it louder or by building to a drop. ...", "The tempo range must be two whole numbers between 30 and 240, lowest first.", "This style mixes instruments tied to different traditions: ... Keep to one tradition, or add the tag 'fusion' to show the mix is deliberate." Parts that are missing are named in words ("The harmony needs a simple, a layered and an intricate version, and is missing: intricate."). At most 200 saved styles, 24,000 characters each.

**Favourites** can be a style (`warm-brush-jazz`), a preset (`warm-brush-jazz/theme-and-return`) or a saved style (`user:evening-porch`); at most 500. Catalog items carry `favorite` and, for presets, `style_favorite`; `favorites=1` keeps both.

**Repeat memory** records each composed prompt (preset, style, arrangement, seed, treatment, fingerprint; the newest 1,000 are kept). Composing with `avoid_repeats` (the default) skips what was used recently, using the windows from the adventurousness slider (table above; 200/6/3 without sliders). If that would leave nothing, **the history rules relax in steps and the result says which**, but the person's own filters never relax:

1. everything recent is avoided (`relaxed: null`);
2. recent arrangement designs are allowed again (`arrangements`);
3. recent styles are allowed again (`styles`);
4. only the last ten presets are avoided (`older-presets`);
5. only the last preset (`last-preset`);
6. nothing (`everything`).

With an empty memory, or `avoid_repeats: false`, the choice is exactly `Library.choose(seed, ...)`. Reading the memory, choosing and recording the choice happen under one lock, so requests that arrive at the same moment cannot both pick what neither has used yet. A preview (`remember: false`) leaves no trace. The memory records a prompt when it is **composed**, not when it is aired: station events carry no prompt provenance, so the memory is a reservation (a composed prompt that is later thrown away still counts).

## Shared Python API

```python
from myradio.prompts import load_library, PromptService, PromptStore, filters_from_sliders, lyrics_scaffold

library = load_library()                       # newest catalog; through="1.0.0" for the original
preset = library.choose(seed=42, tags=["jazz", "acoustic"], energy="calm", exclude_tags=["waltz"],
                        recent_ids=["warm-brush-jazz/theme-and-return"], recent_palettes=["chamber-jazz"])
prompt = library.generate(preset.id, seed=42, complexity="intricate", density="sparse")
record = prompt.to_dict()
# record["style"]: compiled musical direction; record["lyrics"]: None for instrumental music
# record["kind"] == "music"; record["speech"] is False

service = PromptService(PromptStore.open("prompts.sqlite3"))
answer = service.compose({"seed": 7, "sliders": {"energy": 30, "adventurousness": 60, "vocals": 20}})
answer["id"], answer["prompt"], answer["provenance"]       # the stable id and everything to store
```

`Library.browse()` returns ordered preset metadata. Tags use **AND**, exclusions use **ANY**, and query words use case-insensitive **AND** against identity and musical details. Unknown styles/tags yield no matches rather than silently switching to other music. Invalid enums or malformed input raise `ValueError`. `filter_presets()` applies the same rules to any list of presets, including saved ones.

`Library.choose()` uses a seed-local RNG and never mutates global random state or a hidden cursor. It excludes exact recent IDs and, optionally, entire recent palettes. When all choices are exhausted, it raises `NoMatchingPreset`: the pure library never violates exclusions. History is caller-managed there. `PromptService` is the caller that owns durable history and relaxes only the history, as described above.

`Library.generate()` compiles a built-in preset by id; `Library.compose()` compiles any `Preset` object, including a saved style. Instrumental mode rejects lyrics. Vocal mode requires user-supplied nonempty lyrics, preserves them separately, and still targets YuE2. It does not generate lyrics: that is `lyrics_scaffold`, a separate call.

`replay(record)` re-composes a stored built-in record against the catalog version named in it and raises if the fingerprint no longer matches (a changed catalog or an edited record).

### Output and replay

Each record contains:

- Catalog/schema version, stable preset ID (`palette/arrangement`, or `user:<id>`), and constituent IDs.
- Seed, complexity, density, music mode, chosen BPM, and meter.
- Human-readable title, compiled `style`, and separate `lyrics`.
- `kind: music`, `speech: false`, and the music model release ID.
- A SHA-256 fingerprint of the canonical record, excluding the fingerprint itself.

Store the **complete emitted record**, not only its seed. Replaying the same inputs against the same catalog/compiler and Python RNG behavior is deterministic; the test suite checks this across separate processes with different hash randomisation. Future catalog/compiler changes can change output. Bump the catalog version for data changes. A fingerprint identifies an exact prompt, not identical rendered audio or identical perceived music.

Preset names describe the style/form. They are not unique generated track titles, and varying a seed is not a guarantee of a unique composition.

## Station extension

`myradio.prompts.extension.build()` registers the library with the station extension seam. Core playout never calls into it; it consumes no station events. It never starts a model, makes audio or touches the network. Full request and response shapes are in `docs/lanes/prompts.md`.

| Operation | What it does |
| --- | --- |
| `prompts.favorite` (`id`, `on`) | Mark or unmark a style, preset or saved style as a favourite |
| `prompts.save_custom` (`preset`) | Save a copy, an edit or a new style (validated as above) |
| `prompts.remove_custom` (`id`) | Remove a style you saved (`user:` ids only) |

| Route (under `/api/x/prompts`) | What it does |
| --- | --- |
| `GET /catalog` | Paged, filterable list of presets, styles or arrangement designs |
| `GET /search` | Word search across styles, saved styles and arrangement designs |
| `POST /compose` | Choose (or use the named preset), write the prompt, return it with its stable id and provenance |
| `GET /sliders` | What a set of slider positions means |
| `GET /lyrics` | A suggested lyric outline for a preset and seed (remembers nothing) |

## Command line

```
python3 -m myradio.prompts [--catalog 1.0.0] list|generate|export|validate|check|lyrics|sliders ...
```

`list`, `generate` and `export` are as before (`generate --scaffold-lyrics` makes vocal music with a generated outline; `--lyrics-file` supplies your own; the two cannot be combined). `validate` also runs the compatibility rules and compiles every variant. `check` prints the rule report and exits 1 if a rule is broken. `lyrics --preset ID --seed N [--text]` writes an outline. `sliders --energy E --adventurousness A --vocals V` shows what the sliders choose. CLI errors exit 2 on stderr without a traceback or success record. A filtered export with no matches is an error; browsing an empty result is a valid empty array. Exports compile all records before emitting JSON Lines, avoiding partially emitted output on validation errors.

## Bounds and errors

Catalog parsing validates schema versions, matching catalog versions per pack, strictly increasing pack versions, unique IDs, field types, BPM ranges, all three complexity variants, and complete arrangement sections. Shipped data is loaded as package resources, independent of the working directory; the data files sit directly in `data/` so the existing package-data pattern covers them.

Seed range: `0..2^63-1`. The compiler limits a style to 6,000 characters and supplied lyrics to 12,000 characters (a scaffold stays under 2,400). These are local guardrails, **not verified YuE2 token/context limits**. The eventual model adapter must validate the actual tokenizer and input contract, avoid silent truncation, and offer a measured compaction strategy if required.

Request bounds: catalog pages 1 to 100 items (offset up to 100,000), search words at most 8 and 100 characters, tag lists at most 20, `exclude_presets` at most 200, compose bodies at most 128 KiB, saved styles at most 24,000 characters. Errors the service raises on purpose are written for a person and never echo more than 60 characters of what was sent, nor more than five names (a longer list ends "and 35 more"); the same goes for text quoted back from a failed rule. The compiled-style limit applies to saved styles when they are saved (see above), and a saved style that somehow cannot be written out at a setting is left out of the choice, with a warning, instead of failing the request.

## Validation and honest limits

The focused tests compile every preset at every complexity, density, and mode (23,004 combinations), check structural uniqueness and bounds, exercise the compatibility rules (each shown to fire on a deliberately broken style), the sliders table, the lyric scaffold, saved styles, the repeat memory, the station extension and the HTTP routes, and cover history exhaustion, invalid catalog data, and missing lyrics. No models or audio outputs are invoked.

**Not yet verified:** whether YuE2 follows each arrangement, renders uncommon meters (7/8, 9/8, 10/8, 11/8, 12/8), retains counterpoint, honours a named mode or tuning, sings supplied or generated text clearly, accepts either tag spelling, or produces musically satisfactory results. Text mentioning a technique is not proof that a model performs it, and a passing structural or compatibility test is **not an audio-quality claim**. The traditions are not practitioner-reviewed. Do not claim those outcomes until the generation and listening work is done.

Next evaluation should hold the seed/palette stable, compare complexity levels, listen for coherent development and clean endings, and record generated audio/provenance plus user ratings. Failed or over-specified combinations should be revised or disabled rather than advertised as supported. Turbo/Mac inference and packaging remain separate acceptance gates.
