# myradio

A local radio studio. It keeps a station clock running on your computer, plays a queue of audio you have the rights to use, and can ask a music maker and a voice maker on that same computer for new songs and DJ lines. The browser, the built-in operator, and an external agent all use the same controls.

The studio binds to loopback only. It does not host a public station for you.

## Requirements

- Python 3.10 or newer
- A current browser
- FFmpeg on your `PATH` if you want to import MP3, FLAC, Ogg, AAC/M4A or AIFF, or to send MP3 to Icecast. Uncompressed 44.1 kHz, 16-bit PCM WAV does not need it.
- On Windows, the `tzdata` package (installed automatically with this project) so recurring programs and ad flights can use IANA time zones

Making songs with the bundled setup path needs a Mac with Apple silicon, or Linux x86-64 / aarch64 with an NVIDIA driver (`nvidia-smi`). The studio, library, queue, schedules and Icecast output run without that, including on Windows 10 and 11. See [Windows](docs/REFERENCE.md#windows).

## Run a checkout

From this directory:

```sh
python3 -m myradio.server
```

On macOS or Linux you can also open `Start myradio.command`. On Windows, open `myradio.cmd`, or run `py -m myradio.server`.

The process prints a launch code and opens the studio at `http://127.0.0.1:8765/`. The launcher hands the browser a one-time ticket instead of putting the code on the command line. If the browser does not open, visit that address and enter the code. Leave the terminal running. Ctrl+C stops the service. Startup does not start playout, and it does not go live.

```sh
python3 -m myradio.server --no-browser --port 8765 --data-dir "$HOME/.myradio"
```

`--public-origin` allows one exact HTTPS origin for a private reverse proxy. The process still listens only on 127.0.0.1. Details are in [the reference](docs/REFERENCE.md#private-tailscale-preview).

The data directory holds `station.sqlite3`, normalized audio in `media/`, `control.token`, and optional private files for the operator and the Icecast source. Keep it private. Stop the service before you copy it.

## Install

From a checkout, so the commands below are on your `PATH`:

```sh
python3 -m pip install .
```

That installs `myradio`, `myradio-mcp`, `myradio-backup` and `myradio-prompts`. Packaging uses setuptools. The project has no required third-party Python libraries except `tzdata` on Windows.

To install for your user and download the music model, read [`install.sh`](install.sh) and then, if you want that path:

```sh
curl -fsSL https://myradio.garden/install.sh | sh
```

Set `MYRADIO_SKIP_SETUP=1` to install the program without running `myradio setup`. `myradio setup` is the step that downloads the model and makes a test song. It is not required to play audio you already have.

## Use the studio

The page is a mixing desk.

1. **Import audio** you are allowed to play (＋ Add audio, or the library sheet). Music joins rotation. Spoken links and ads play only when queued or scheduled. Each file and its decoded PCM must be within 100 MiB, and at least 100 ms.
2. Drag carts on the wall, or use the keyboard: Space picks a cart up, the arrow keys move it, Space puts it down, Escape cancels, Delete removes it from the queue.
3. **Start station** runs local playout. **Listen here** monitors it in this browser. Those are separate. Closing the browser does not stop the station. The monitor volume stays available on a narrow screen.
4. **Stop** ends playout and puts the current track back at the front of the queue. If the queue is already full, the last cart is dropped so the track that was on air can return. **Fade** fades the current track out over two seconds and plays the next cart. The queue holds at most 1,000 carts.
5. **Schedule** one-off times and weekly programs. They join the queue after the current track. They do not cut in on the second. An event more than 60 seconds late, or due while the station is stopped, is marked missed and is not replayed in a burst.
6. **Go live** is a separate guarded action. It sends 128 kbit/s MP3 to an Icecast mount you configure. Saving a destination does not connect. Remote mounts need HTTPS. Plain HTTP is only for this computer.
7. **Autopilot DJ**, on the studio page, places host breaks, station IDs and news between songs when a voice maker is available. It stays off until you turn it on. The four pads name those placements.
8. The maker line under the paper trace reports the music and voice makers. With none configured, imported audio still plays.
9. **Operator** is a local command assistant (`start`, `stop`, `skip`, `status`, `name …`) until you connect an OpenAI-compatible tool-calling model. That model may propose changes. Each proposal waits for your approval. No audio is sent to it.
10. **Menu → Connect your agent** copies an MCP configuration for an agent on this computer. The text has no secret in it. The adapter reads the control token from the data directory. External agent tools act immediately. Say what the agent may and may not do.
11. **Lock studio** drops this browser's session. It does not stop playout or a broadcast that is already live.

Dial, the level and the trophies are scored from the station's own counters (time on air, items played, fades, the library, the stream and the agent link).

## Agents, backup and the rest of the contract

- HTTP and MCP operations, signed requests, sessions, schedules and broadcast rules: [docs/REFERENCE.md](docs/REFERENCE.md)
- Prompt catalog: `python3 -m myradio.prompts --help` and [docs/PROMPTS.md](docs/PROMPTS.md)
- Hermes and omp plugins: [integrations/](integrations/)
- Portable backup (stop the service first; credentials are not included):

```sh
python3 -m myradio.backup create --data-dir "$HOME/.myradio" "$HOME/station-backup.myradio.zip"
python3 -m myradio.backup restore "$HOME/station-backup.myradio.zip" --data-dir "$HOME/.myradio-restored"
```

Hardware notes from the November 2026 runs live in [docs/research/benchmarks-2026-11-06/](docs/research/benchmarks-2026-11-06/). This file does not restate them.

## Develop

```sh
python3 -m pip install -e ".[dev]"
python3 -m unittest discover -s tests -p "test_*.py"
node --check myradio/web/app.js
```

`.[dev]` installs setuptools so the packaging test can build a wheel and check that host data and the studio files are inside it. The tests are the standard library `unittest` suite under `tests/`. GitHub Actions runs that command on Ubuntu with Python 3.12. See [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

Some broadcast tests speak to a real Icecast binary only when `MYRADIO_TEST_ICECAST` points at one. Without it they skip that case. They do not count a missing binary as a pass of the live path.

## License

myradio is open source under the Apache License 2.0. See [LICENSE](LICENSE). Copyright 2026 Omnispace Technologies and the myradio contributors.

The program does not include model weights. Those are downloaded separately and keep their own licenses. [NOTICE](NOTICE) lists the ones this project is set up to use. Songs made with YuE2 are CC BY-NC 4.0: they may be shared, and they may not be sold.
