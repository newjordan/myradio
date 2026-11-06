# Lane: audio (`myradio/audio/`)

A pure-Python PCM toolkit that the generation pipeline and later features use to **validate, convert and polish audio**. It is a library, not an Extension: there is no `extension.py`, it is not in `extensions.BUILTIN`, and it has no operations, routes, status or events.

Owned paths: `myradio/audio/**`, `tests/test_audio*.py`, `docs/lanes/audio.md`. Python standard library only (no `audioop`, which Python 3.13 removed; no third-party imports; a test enforces this).

## 1. Purpose and honest status

**Real (executed and observed in this session)**

* Every function in the frozen contract and the additions in section 2.
* 177 deterministic tests, all passing on Python 3.12.3 and on Python 3.11.15 (section 7).
* The fixed-point filter engine is bit-for-bit identical to a plain integer loop on every case tried, including heavy clipping.
* Loudness agrees with FFmpeg's `ebur128` filter to 0.009 LU or better on 16 synthetic signals (section 7). FFmpeg was used only as a one-off reference run from the scratch folder; no test needs it.
* `write_wav` output is accepted by Python's `wave` module, by the station's own strict playout preflight (`myradio.media.open_media`), and by a real `Station` (created without its clock thread) through `Station.import_audio` as speech, ad and music.
* Hostile-file handling was checked against WAV files built by `struct` in the tests (not by the reader under test), 1,200 mutated headers, 500 random byte strings, random sound data in every sample format, and every possible truncation of a valid file.
* Four-minute-song timings and memory use were measured in fresh processes (section 7).

**Verified only on synthetic stand-ins**

* All audio in every test is synthetic: sine, square, DC, white noise and silence from `myradio.audio.synth` or `struct`. **No real YuE2 or Drama 3 output was ever processed.** The quality-gate thresholds (section 2, `validate_generated`) are engineering defaults chosen from facts in the model cards kept under `.reference/` (the YuE2 card's playlist test counts 86,400,000 stereo frames in 1,800 s, which is 48 kHz, and it notes rail samples and naturally quiet tails; the Drama 3 card says its stream is 44.1 kHz mono) and checked on synthetic signals only. They must be re-checked on real output before anyone relies on them to reject or accept programming.

**Unverified**

* Python 3.10 and 3.13 interpreters do not exist on this machine. The 3.10 grammar was checked with `ast.parse(..., feature_version=(3, 10))`; the code uses `math.sumprod` only when it exists; nothing was executed on 3.10 or 3.13.
* macOS, and big-endian hosts (the byte-order paths are written but cannot be exercised here).
* Anything about how the audio sounds. No listening test was done.
* Behaviour under a real station load beyond the stall measurement in section 7.

## 2. Public API

All names are importable from `myradio.audio`. Frozen names are exactly as the contract specifies; everything else is an addition.

### Types

| Name | Shape |
| --- | --- |
| `AudioError(ValueError)` | `AudioError(message, code="invalid", detail="")`. `str(e)` is a plain-language message safe to show to a person. `e.code` is one of `invalid, not_wav, damaged, truncated, unsupported, channels, rate, empty, too_large, missing, unreadable, unwritable, internal`. `e.detail` is a technical remark for logs only. Survives `pickle`. |
| `Pcm` | Frozen dataclass `Pcm(frames: bytes, rate: int, channels: int, notes: tuple = ())`. `frames` is interleaved signed 16-bit little-endian. `channels` is 1 or 2 and `rate` 8,000 to 384,000; anything else, or a length that is not a whole number of frames, raises `AudioError` on construction. `bytearray` and `memoryview` are copied into `bytes`. Properties `frame_count` and `seconds`. Equality and hashing use `frames`, `rate`, `channels` (not `notes`). `repr` is short (never prints the audio). `notes` holds up to 12 plain-language remarks about repairs or limits (see section 5). `with_notes(*text)` returns the same audio with more notes. |
| `Analysis` | Frozen dataclass: `duration_s, peak_dbfs, rms_dbfs, loudness_lufs, silence_ratio, clipped_ratio, dc_offset, leading_silence_s, trailing_silence_s`, plus `as_dict()`. Every number is finite; silence reads `-120.0` for the dB fields. |
| `Problem` | Frozen dataclass `Problem(code, message)` with `code` in `silent, too_short, too_long, clipped, dc_offset, truncated_tail`, plus `as_dict()`; `str(problem)` is the message. |
| `NormalizeReport` | Frozen dataclass: `applied, input_lufs, target_lufs, gain_db, expected_lufs, peak_before_dbfs, peak_after_dbfs, limited_by, message`. `limited_by` is `None`, `"ceiling"` or `"boost_cap"`. |

### Functions (frozen contract)

| Call | Returns | Behaviour |
| --- | --- | --- |
| `read_wav(source, max_bytes=104857600)` | `Pcm` | `source` is a path (`str` or `PathLike`) or the file's bytes (`bytes`, `bytearray`, `memoryview`). Integer sound of 8, 16, 24 and 32 bits (nearest-rounded and saturated to 16 bits), 32- and 64-bit float (clamped, non-numbers replaced, and reported in `notes`), WAVE_FORMAT_EXTENSIBLE with the standard PCM or float sub-format, mono or stereo. Raises `AudioError` for everything else (compressed formats, unknown extensible sub-formats, more than two channels, zero or out-of-range sample rate, a block size that contradicts the format, a missing or repeated format block, the sound before the format, an unknown-length data size (`0xFFFFFFFF`), an empty sound, sound that is cut short, files over the limit, RIFX/RF64, folders, pipes and other special files). The RIFF size and the average-bytes-per-second field are ignored; trailing bytes after the sound are ignored; odd-sized chunks are padded to even, and a missing pad byte is tolerated; a trailing partial frame is dropped with a note. |
| `write_wav(pcm)` | `bytes` | Canonical 44-byte header (`fmt` size 16, PCM, 16-bit) followed by the frames. |
| `to_station_format(pcm)` | `Pcm` | 44.1 kHz stereo. Returns the same object when already in that format. Mono is played from both speakers (the one channel is copied to both). Other rates go through `resample` (`"good"`). |
| `resample(pcm, rate, quality="good")` | `Pcm` | Same duration and channels at the new rate; length is `round(frames * new / old)`. `quality` is `"good"` (default) or `"fast"`. Same rate returns the same object. |
| `analyze(pcm)` | `Analysis` | See definitions below. |
| `validate_generated(pcm, role, min_s, max_s)` | `list[Problem]` | Empty when fine. `role` in `song, host_break, id, spot, update` (only changes wording). `min_s` and `max_s` may be `None`. Cheap: no loudness measurement. |
| `normalize(pcm, target_lufs=-16.0, ceiling_dbfs=-1.0)` | `Pcm` | One constant gain; never clips. See below. |
| `fade(pcm, in_ms, out_ms)` | `Pcm` | Linear fades; length unchanged. (`in_ms` and `out_ms` default to 0.) |
| `trim_silence(pcm, threshold_dbfs=-50.0, keep_ms=60)` | `Pcm` | Cuts silence from both ends. |
| `concat(parts, crossfade_ms=0)` | `Pcm` | Joins end to end, optionally with equal-power crossfades. |

### Additions

| Call | Purpose |
| --- | --- |
| `write_wav_file(pcm, path) -> int` | Saves a WAV atomically (temporary file in the same folder, then rename), readable only by its owner. Raises `AudioError("unwritable")`; a failed save leaves any old file untouched. |
| `apply_gain(pcm, gain_db)` | Level change from -120 to +40 dB; louder results saturate at full scale (no wrap-around) and a note says how many samples were cut off. |
| `normalize_with_report(pcm, target_lufs, ceiling_dbfs) -> (Pcm, NormalizeReport)` | `normalize` plus the numbers. |
| `loudness_lufs(pcm) -> float` | Gated integrated loudness only. |
| `selfcheck() -> {"ok", "message", "seconds"}` | About 0.1 s end-to-end check on this computer. Never raises. |
| `myradio.audio.synth` | `sine, square, constant, noise, silence`: synthetic test signals (not music, not speech, not model output). |
| Constants | `STATION_RATE=44100`, `STATION_CHANNELS=2`, `MIN_RATE`, `MAX_RATE`, `MAX_PCM_BYTES`, `MAX_WAV_BYTES`, `MAX_BOOST_DB`, `FLOOR_DB=-120.0`, `PROBLEM_CODES`, `ROLES`, `QUALITIES`. |

Internal modules (importable, not part of the contract): `pcm` (types), `wav`, `rates` (named `rates` so that `myradio.audio.resample` is only ever the function), `lanes` (fixed-point engine), `loudness`, `analysis`, `edit`, `check`.

### Definitions

* **dBFS** is relative to a sample of +-32768 (so a full-scale 16-bit sample is 0 dBFS). Peak is the sample peak, not an inter-sample ("true") peak.
* **`analyze`**: `duration_s = frames/rate`. `rms_dbfs` is over every sample of every channel. `loudness_lufs` is below. `silence_ratio` is the fraction of frames in 20 ms blocks whose samples are all at or below -50 dBFS. `clipped_ratio` is the fraction of samples exactly at +32767 or -32768. `dc_offset` is the largest absolute channel average divided by 32768. `leading_silence_s` and `trailing_silence_s` are the time before the first and after the last sample louder than -50 dBFS (both equal the whole duration for silent audio). Empty audio gives all zeros and -120 dB.
* **`validate_generated` rules** (constants in `analysis.py`): `silent` when the peak is below -60 dBFS or at least 98% of 20 ms blocks are silent (or the audio is empty); `too_short` / `too_long` against `min_s` / `max_s`; `clipped` when more than 0.1% of samples sit at the rails (a few rail samples in a peak-normalised file are fine); `dc_offset` when any channel average exceeds 2% of full scale; `truncated_tail` when the loudest sample in the last 10 ms is above -50 dBFS **and** less than 15 dB below the clip's own peak (a natural ending, a pause or a fade of about 100 ms or more is well below that; a cut-off is not). A silent clip reports only `silent` (and the length codes). Order of the list: `silent, too_short, too_long, clipped, dc_offset, truncated_tail`. A bad `role`, or `max_s < min_s`, raises `AudioError`.
* **Loudness (`loudness_lufs`, `Analysis.loudness_lufs`)**: K-weighting (the BS.1770 high-shelf and 38 Hz high-pass, designed per sample rate; at 48 kHz the coefficients equal the printed ones to 9e-16), 400 ms blocks every 100 ms, `-0.691 + 10 log10(sum of channel mean squares)`, absolute gate -70 LUFS, relative gate 10 LU below the average of the blocks that passed. **Mono is measured as both speakers** (the station plays it that way), so a mono file reads 3.01 LU louder than a tool that counts one speaker (FFmpeg's `ebur128` does). Audio shorter than 400 ms is measured as one block. Nothing above the gate reads `-120.0`. The result is memoised on the `Pcm`.
* **`normalize`**: measure gated loudness; `gain = target - loudness`, but never more than +40 dB, and never so much that the sample peak would pass `ceiling_dbfs` (the highest allowed sample is `min(floor(32768 * 10^(ceiling/20)), 32767)`). A constant gain is applied with round-to-nearest; there is no limiter, compressor or clipping, so the shape of the audio is unchanged. When the ceiling (or the boost cap) rather than the target set the gain, the result is quieter than the target, `NormalizeReport.limited_by` says which, and a plain-language note is added to `Pcm.notes`. Silent audio, and audio below the -70 LUFS gate, comes back unchanged with a note. `ceiling_dbfs` must be between -30 and 0, `target_lufs` between -70 and 0. The expected loudness after the change is `report.expected_lufs`; measuring the result again costs a full loudness pass.
* **`fade`**: gain `k/N` for frame `k` of an `N`-frame fade-in and `(N-1-j)/N` for frame `j` of a fade-out, so the first frame of a fade-in and the last frame of a fade-out are zero; the untouched middle is byte-identical; fades that together exceed the audio are shortened in proportion; maximum 60 s each.
* **`trim_silence`**: a sample is sound when it is louder than the threshold in any channel. Leaves `keep_ms` (default 60 ms) of silence each side (or what exists). Silence inside is untouched. Audio that is silent end to end becomes **empty** (0 frames) with a note; check `frame_count` before using the result.
* **`concat`**: all parts must share rate and channels (else `AudioError` pointing at `to_station_format`). Without crossfade the frames are joined byte for byte. With `crossfade_ms`, each join overlaps by `join = min(round(ms*rate/1000), len(a)//2, len(b)//2)` frames and mixes the faded-out tail of `a` and the faded-in head of `b` with an equal-power curve (`cos`/`sin` of `(i+0.5)/join * pi/2`, saturating at the rails). Result length is `sum(lengths) - sum(joins)`, so for ordinary parts it is exactly `sum(lengths) - (parts-1) * overlap`. Notes are merged without repeats. The total may not exceed 100 MiB.
* **`resample`, default quality, and why**: `"good"` is a windowed-sinc polyphase filter: Kaiser window (beta 8.6, about 87 dB), 32 zero crossings each side (64 taps per phase when upsampling; more when downsampling so the sharpness is kept), cutoff placed so the stopband starts exactly at the new Nyquist, every fractional position has its own taps normalised to unit gain at DC, tables rounded to 22 bits and cached per ratio (`rates.build_plan`, 4 ratios kept). It is the default because everything the station plays is 44.1 kHz, so an aliasing or imaging error would be heard on air every time, and the cost is a few seconds per song. `"fast"` is linear interpolation with **no anti-aliasing**; it is for previews and for audio with nothing near the top of its band. Rates that reduce to at most 1,024 phases (every ordinary one: 8, 11.025, 12, 16, 22.05, 24, 32, 44.1, 48, 88.2, 96, 176.4, 192, 384 kHz and their pairs) are converted exactly. A rate with a larger denominator (for example 44,101 Hz) uses the nearest ratio with at most 1,024 phases; the speed error is at most 0.05% (usually far less) and a note says so when it exceeds one part per million. Band-limited conversion can raise the sample peak (it recreates the peaks that fell between the old samples); anything that would pass full scale is clamped and counted, and a note says how many samples were cut off.

## 3. Persistence

None. The package keeps no database, no state directory and no configuration; nothing here belongs in a backup and nothing is secret.

In memory only: filter tables (`rates.build_plan`, 4 ratios), lane constants (`lanes._constants`, 12 sizes), K-weighting coefficients (16 rates), and a small per-`Pcm` cache of loudness and scan results. All are bounded and rebuilt on demand.

On disk only when the caller asks: `write_wav_file(pcm, path)` writes `path`, via a temporary file named `.audio-*.tmp` in the same folder. The temporary file is removed on failure; a process killed in the middle could leave one behind, and it is safe to delete. Callers (the generation lane) should store `Analysis.as_dict()`, `NormalizeReport` fields and `Pcm.notes` with each item's own provenance if they want them later.

## 4. Extension surface

There is none by design: no operations, routes, panels, status, events consumed, or backup files. The station core never calls this package, and this package never calls the station.

Suggested use by other lanes:

* **Generation** (per finished job): `pcm = read_wav(path)`; `problems = validate_generated(pcm, role, min_s, max_s)` (fail the job with `problems[0].message` if any); `pcm = to_station_format(pcm)`; `pcm = trim_silence(pcm)` (check `frame_count`); `pcm = fade(pcm, in_ms, out_ms)`; `pcm, report = normalize_with_report(pcm)`; `station.import_audio(write_wav(pcm), title, artist, kind)`. YuE2 songs arrive at 48 kHz stereo and Drama 3 speech at 44.1 kHz mono, so the song path includes a real 48 to 44.1 kHz conversion. Library kinds follow the shared vocabulary: `song` imports as `music`; `host_break`, `id` and `update` as `speech`; `spot` as `ad`.
* **Order matters: convert first, then set the level.** The ceiling given to `normalize` is a promise about the samples it sees. A sample-rate change afterwards can raise the sample peak: converting 48 kHz material to 44.1 kHz changed the peak by +4.0 dB for white noise, +1.9 dB and +2.5 dB for 1 kHz and 5 kHz square waves, and about 0 dB for sine waves (measured). Normalising first and converting second can therefore leave the broadcast peak above the ceiling, or clip at full scale (the conversion then reports it in `notes`). With the order above, the ceiling holds for the audio that goes on air.
* **Doctor**: add one readiness row from `selfcheck()` (`ok` true is green; `message` is already plain language).
* **Threads**: every function is pure, thread-safe and CPU-bound. Run them on the generation worker's own thread or process, never on the playout clock. Python loops yield normally; the longest single stalls seen are large one-piece memory copies and long C-level passes over big arrays (section 7).

## 5. UI contract

The audio package draws no screens, but several of its strings reach screens. All are plain language and end in a next step.

* Show `str(AudioError)` and `Problem.message` as they are; never show `e.detail`, which is for logs. For a failed generated item the screen should offer "Try again". `AudioError.code` lets a screen choose an icon or a hint:

| code | Meaning | Example message |
| --- | --- | --- |
| `not_wav`, `damaged`, `truncated`, `empty` | The file is not usable audio | "The audio file looks cut short: it should hold 3 minutes 5 seconds of sound but only 1 minute 12 seconds is there. If it was still being saved, wait for it to finish; otherwise create it again." |
| `unsupported`, `channels`, `rate` | A format the station cannot read | "This audio has 6 channels. The station can use mono or stereo; export it as stereo and try again." |
| `too_large` | Over the 100 MB limit | "This audio file is 120 MB, more than the 100 MB the station accepts. Use a shorter recording." |
| `missing`, `unreadable`, `unwritable` | File access | "The audio file could not be found. Check the name and try again." |
| `invalid` | A bad request (usually a programming error) | "The fade-in length must be a number from 0 to 60000 milliseconds." |

* `Problem` messages, for example: "The song came out silent: there is nothing to hear. Try generating it again." and "The host break seems to stop abruptly, as if it was cut off. Try generating it again, or fade out its end." Roles read as song, host break, station ID, sponsor message, news update.
* `Pcm.notes` and `NormalizeReport.message` are optional small "heads up" lines under an item, for example "The sound could not reach -16.0 LUFS without going above the -1.0 dBFS peak ceiling, so its level was changed by +4.2 dB instead of +9.0 dB and it should measure about -20.8 LUFS." Primary screens should say "quieter than the station's usual level" rather than print LUFS; the numbers belong behind a details link.
* Suggested plain labels for `Analysis`: `peak_dbfs` "Loudest moment", `loudness_lufs` "Overall loudness", `silence_ratio` "Quiet parts", `clipped_ratio` "Distortion", `leading/trailing_silence_s` "Silence at the start/end". `dc_offset` is diagnostic and can stay hidden.
* A quality check with no problems should say so ("No problems found.") rather than show an empty list.

## 6. Limits and bounds

| Limit | Value | Where |
| --- | --- | --- |
| WAV file size (read) | 100 MiB default; caller may raise to 1 GiB. Applies to the file and to the converted 16-bit audio; checked before any sound is read | `read_wav` |
| Sample rate | 8,000 to 384,000 Hz | `Pcm`, `read_wav`, `resample` |
| Channels | 1 or 2 (more is rejected) | everywhere |
| Result size of `resample`, `to_station_format`, `concat` | 100 MiB, refused before any work | those calls |
| Work guard for one conversion | output samples x taps at most 5e9 (about 45 s at the worst measured cost of 9 ns per tap, 384 kHz to 8 kHz), else "cut it into shorter parts" | `resample` |
| Format block | 16 to 1,024 bytes | `read_wav` |
| Chunk headers walked before the sound | 256 | `read_wav` |
| Fades / crossfade / silence to keep | 60 s / 30 s / 60 s | `fade`, `concat`, `trim_silence` |
| Gain | -120 to +40 dB; `normalize` boost capped at +40 dB | `apply_gain`, `normalize` |
| Filter phases | at most 1,024 per ratio | `rates` |
| Notes per `Pcm` | 12 | `Pcm` |
| Working memory of one filter segment | window integers about 16 MB at most, 4,096 samples per integer | `lanes` |
| Time | linear in the audio. Four-minute stereo song: analyze about 5 s, normalize about 5 s, 48 to 44.1 kHz about 4 s (section 7). At the 100 MiB cap (52 million mono samples, 19.8 minutes) the loudness measurement took 8.8 s and `validate_generated` 2.5 s (measured). The slowest read is a float file of random garbage, about 0.13 microseconds per sample (3.4 s for 100 MiB) | |
| Peak extra memory | read 16-bit: the file once; 24-bit and float: about 2x the converted audio; `to_station_format` of a 48 kHz stereo song: about 2x the output on top of the input; mono to stereo: about 5.5x the mono input (110 MB for a 20 MB four-minute mono track) | measured, section 7 |
| Threads, processes, sockets, files left behind | none | |

## 7. Test inventory, commands and measured results

Command (runs every test file this lane owns):

```
cd /home/frosty40/myradio && nice -n 19 ionice -c3 timeout 900 python3 -m unittest discover -s tests -p "test_audio*.py" -v
```

**Result: 177 tests, 0 failures, 0 errors, about 9 s on a busy 32-thread machine (Python 3.12.3); the same 177 pass on Python 3.11.15 in about 9 s; they also pass with `-W error -X dev` (no warnings, no unclosed files).** Whole suite (`python3 -m unittest discover -s tests -p "test_*.py"`, 2,206 tests, 374 s, run once at the end while other lanes were still being built): every audio test passed; the one failure belonged to another lane (`test_generation_core` found an unfilled `{{RESULTS}}` marker in that lane's own contract doc), with 1 skipped test and 10 expected failures that are not in this lane.

| File | Tests | Covers |
| --- | --- | --- |
| `test_audio_wav.py` | 47 | WAV zoo (8/16/24/32-bit, float32/64, extensible, mono/stereo, eight rates); rounding; float repair and notes; every hostile case (lying RIFF and data sizes, zero rate, absurd channels, unknown sub-formats, odd chunks, trailing garbage, truncation at every byte, giant or repeated format blocks, chunk floods, 1,200 header mutations, random bytes, random sound data); size limit before reading, sparse file, shrinking file, FIFO, folders, NUL in paths; `write_wav` against `wave`, `media.open_media` and a real `Station`; atomic file saving; `Pcm` rules; error pickling; no forbidden imports |
| `test_audio_dsp.py` | 74 | contract signatures and fields; `analyze`; every `Problem` code; fades; trimming; joining and crossfades (exact lengths, curve values, saturation); `normalize` (target, ceiling, boost cap, never clipping, silent input); gain; `selfcheck`; memory exhaustion; pickling, copying and JSON; synthetic signals; zero-to-five-frame audio through every function |
| `test_audio_loudness.py` | 22 | filter coefficients against the standard; 997 Hz, other frequencies and rates; FFmpeg reference values; white noise against the integrated response; gating (relative, absolute, power averaging); short clips; floors, JSON safety, memoisation |
| `test_audio_resample.py` | 34 | lane engine bit-exact against a naive integer loop (11 rate pairs, noise/square/tiny signals, 7 segment sizes); gain plan bit-exact; unit gain; lengths for 72 rate/duration pairs; pitch by zero crossings; passband flatness; alias and image rejection; exact sine reproduction and no time shift; fast mode; channel separation; saturation and the exact count of cut-off samples; ratio approximation; guards; plan cache; twelve threads sharing audio and tables |
| `test_audio_support.py` | 0 | helpers (`struct`-built WAVs, tone and spectrum measurements) |

Not in the suite (done once from the scratch folder, results below): FFmpeg comparison, four-minute timings and memory, thread stall measurement, a 1,200-pipeline random stress run (4,131 operations, no exception, every length invariant held) and a 600-case random check that `normalize` never passed its ceiling (157 were ceiling-limited, no violation, no sign flip).

### Measured accuracy

| Claim | Measured |
| --- | --- |
| K-weighting coefficients at 48 kHz vs the printed standard | max difference 8.9e-16 |
| 997 Hz sine, -20 dBFS peak, both speakers (analytic value -20.000 at 48 kHz) | 48 kHz -19.9994; 44.1 kHz -19.9967 (analytic for that rate's filter -19.9972); 8, 16, 22.05, 32, 96 kHz all within 0.05 of -20.0 (8 kHz -19.985, 96 kHz -20.017) |
| Same tone at 12 rates from 8 kHz to 192 kHz against the analytic value of that rate's own filter | within 0.0006 LU at every rate (that is the rounding of the test tone's amplitude) |
| Sines at 50, 100, 400, 3,000, 8,000, 15,000 Hz at 44.1 and 48 kHz against the filter response | within 0.002 LU |
| 16 signals against FFmpeg `ebur128` (sines 40 Hz to 15 kHz, 44.1 and 48 kHz, white noise, a random tone mix, gated programme-like sequences) | worst 0.009 LU, typical 0.005 (this implementation reads about 0.005 higher) |
| Mono vs FFmpeg | differs by 3.0 LU by design (this module counts both speakers) |
| Resampler passband (`good`, 48 to 44.1 and 96 to 44.1) | flat to 0.001 dB up to 18 kHz; -0.25 dB at 19 kHz; -4.4 dB at 20 kHz |
| 24 to 44.1 kHz passband | flat to 0.004 dB up to 10 kHz |
| Alias rejection, 48 to 44.1 kHz | a 23 kHz tone leaves 91 dB down at 21.1 kHz; 22.2 kHz leaves 86 dB down at 21.9 kHz |
| Image rejection when upsampling | 92 to 125 dB (24 kHz source, 5 kHz tone: 101 dB at 19 kHz) |
| Waveform accuracy | converted sines match the ideal sine at the new rate within 0.5 to 1.1 steps of the last bit (rounding only) at eight rate pairs; an impulse lands where it should (no delay) |
| `fast` mode | exactly linear interpolation; the error on a 1 kHz tone from 24 kHz is about 0.9% of its amplitude |
| Lane engine vs naive loop | identical on every case |

### Measured timings and memory

Fresh process per measurement, best of three, `nice -n 19` on a shared 32-thread machine with a load average near 6 (so these are conservative). The "song" is one second of noisy tones tiled 240 times (a real song's content does not change the cost, except that peaks near full scale make the filter's overshoot check more likely). "Extra memory" is the peak above the input already held.

| Operation | Python 3.12.3 | Python 3.11.15 | Extra memory (3.12) |
| --- | --- | --- | --- |
| `read_wav` 4 min 44.1 kHz stereo 16-bit, from a path | 0.04 s | 0.03 s | 40 MB (the audio itself) |
| `read_wav` 24-bit | 2.41 s | 2.23 s | 83 MB |
| `read_wav` float32 | 2.96 s | 2.26 s | 83 MB |
| `validate_generated` | 1.09 s | 0.93 s | 3 MB |
| `analyze` (includes loudness) | 5.20 s | 5.08 s | 3 MB |
| `normalize` (measures loudness) | 5.14 s | 4.76 s | 89 MB |
| `apply_gain` | 0.60 s | 0.62 s | 88 MB |
| `fade` | 0.11 s | 0.11 s | 46 MB |
| `trim_silence` (nothing to trim) | 0.00 s | 0.00 s | 0 MB |
| `concat` two songs, 2 s crossfade | 0.14 s | 0.13 s | 85 MB |
| `write_wav` | 0.04 s | 0.04 s | 40 MB |
| `to_station_format`, 4 min 48 kHz stereo to 44.1 kHz, `good` | 3.58 s | 4.01 s | 85 MB |
| same, `fast` | 0.59 s | 0.59 s | 82 MB |
| `resample` 4 min 44.1 to 96 kHz, `good` | 6.93 s | 8.14 s | 182 MB |
| `to_station_format`, **30 s mono voice 24 kHz to 44.1 kHz stereo**, `good` | **0.22 s** | 0.25 s | 18 MB |
| same, `fast` (resample only) | 0.03 s | 0.03 s | 6 MB |
| `analyze` 30 s mono voice | 0.17 s | 0.18 s | 1 MB |

Why it is fast: a per-sample Python loop costs about a microsecond per output sample. The resampler and gain instead pack thousands of samples side by side in one big integer and multiply by each filter tap in a single C-speed operation (about 2.3 ns per tap per output sample, against about 21 ns for the fastest per-sample alternative tried, `math.sumprod` on Python 3.12, and about 31 ns for `sum(map(mul, ...))`). The loudness filter is inherently sequential and runs as a tight Python loop (about 0.4 microseconds per stereo frame).

Thread stall (a 1 ms ticker thread in the same process while a four-minute operation ran; the longest delay seen, Python 3.12.3, final code): `validate_generated` 8 ms, `analyze` 9 ms, `fade` 28 ms, `apply_gain` 41 ms, `normalize` 43 ms, `to_station_format` 43 ms, reading plus writing 46 ms, `concat` 55 ms; an idle baseline 0.6 ms. The cause was not isolated for every row. What the pattern shows: before the scan pass was cut into smaller pieces (65,536 frames) `analyze` stalled the ticker for 23 ms and `validate_generated` for 11 ms, so a single C-level pass over a few hundred thousand samples is enough to hold the interpreter lock for that long; the 40 to 55 ms rows are the ones that build a 40 to 80 MB result in one piece (a single copy that gives other threads no turn). For context, the station's playout clock works in 100 ms chunks and only resets its deadline after falling 0.5 s behind (clock loop in `station.py`, read, not tested here), so these stalls are well inside its slack; still, run the toolkit off the playout thread.

## 8. Requested changes to shared files

None are required. Suggestions only (not made, because those files belong to others):

* `pyproject.toml`: nothing. `[tool.setuptools.packages.find] include = ["myradio*"]` already picks up `myradio.audio`; there is no package data.
* Doctor lane: optionally call `myradio.audio.selfcheck()` for an "audio tools" readiness row.
* Generation lane: use the pipeline in section 4, in particular convert with `to_station_format` **before** `normalize_with_report` (as read while this was written, `finish()` in `myradio/generation/audio_api.py` normalises first and converts last, which can leave the final peak above the ceiling or clip it); keep `Analysis.as_dict()` and the normalize report with each item's provenance.
* `docs/HANDOFF.md` or `README.md`: a one-line mention that `myradio/audio/` exists and is a library (no operations), if a source map is kept.

## 9. Known limitations and unverified claims

* **No real generated audio was tested.** The quality-gate thresholds are defaults from synthetic tests and the model cards, not tuned on YuE2 or Drama 3 output; false alarms and misses are possible.
* **The abrupt-ending check cannot see every cut-off.** A clip that is cut off during a passage more than 15 dB below its loudest moment, or quieter than -50 dBFS, is not flagged; an unusually short fade (under about 100 ms) is flagged even though it is deliberate.
* **Sample peak only.** `normalize` limits the sample peak, not the inter-sample peak. A lossy encoder (the Icecast output is 128 kbit/s MP3) can overshoot a -1 dBFS sample peak by a fraction of a dB; the default ceiling leaves that room. No true-peak meter is included.
* **No dither** is applied when rounding to 16 bits; rounding noise sits near -98 dBFS, and an extremely quiet passage can show rounding distortion.
* **Streaming WAV headers are refused.** A file whose sound length reads as unknown (`0xFFFFFFFF`) or zero is treated as damaged or empty (a recording that was still being made looks the same as a lie). A caller that knows its data is complete can build `Pcm(raw, rate, channels)` itself.
* Truncation is strict: a data size larger than the file is an error even if the missing part is one byte. A data size smaller than the file is honoured (the rest is ignored).
* Loudness is an approximation of BS.1770 validated on synthetic signals and against FFmpeg's implementation, not against the official EBU conformance files (which are not on this machine). Filters for rates other than 48 kHz use the bilinear transform (like FFmpeg and libebur128), which is why the 997 Hz tone reads up to 0.04 LU away from the nominal -20.0 at some other rates. Mono counts as both speakers (section 2).
* `silence_ratio`, silent edges and `trim_silence` look at sample peaks, so a very low hum below the threshold counts as silence and a single click above it counts as sound.
* Unusual sample rates that need more than 1,024 filter phases are converted with a nearby ratio (at most 0.05% speed error).
* `to_station_format` of a mono clip to stereo and the final copy of every result make several full copies of the audio (about 5x a mono input at the worst, see section 6). Fine for songs and spots; do not call it on hours of audio.
* Heavy conversions hold Python's global lock during large memory copies for up to about 55 ms at the four-minute size (measured; the cause was not isolated for every operation), and the K-weighting loop is pure Python and competes for the lock like any other Python code.
* Big-endian machines, macOS, Python 3.10 and 3.13 were not run (section 1). The 30 s voice timing is from this machine only.
* Hash-based `Pcm` equality compares the full audio, which costs a pass over the bytes the first time it is hashed.
* Filter tables are computed with the platform's math library, so converted audio can differ by one least-significant bit between machines.
* `selfcheck` proves the tools work, not that any particular file will; it is not a substitute for `validate_generated`.
