# myradio

**Turn your own computer into a live radio station, with new songs made by YuE2 and your own agent running the show.**

<!-- AD VIDEO: Embed the approved “NOW THIS IS MyRadio” launch ad. [NEEDS: approved ad embed] -->
<!-- SCREENSHOTS: Show the Console studio, the music and DJ crates, and Connect your agent. -->

## What it is

myradio is an open-source app for a station of your own. YuE2 is the AI music model that makes new songs from your prompts and lyrics. A DJ voice talks between songs. Your agent, an AI assistant you connect, picks what comes next and writes the DJ’s words. myradio makes and plays the music, then speaks those lines.

Run the show from the Console, a browser studio laid out like a mixing desk. Keep music and DJ clips in separate crates. Dial, the pixel-art radio mascot, keeps you company.

[myradio.garden](https://myradio.garden)

## Everyone gets their own bard

- **Frank’s game night, sung live.** During Frank’s Dota stream, an agent grabbed screenshots and asked the station for songs as he played: “Riki with a Dagon”, “Boots of Travel” and “They Can’t Doubt the Hands”.
- **Your chat, sung live.** Your agent reads Twitch chat and asks myradio for songs about the shout-outs and inside jokes.
- **Your notes, in a song.** Give your agent work or study notes to turn into songs.
- **Your family party, in the lyrics.** Ask for dedications and songs about shared memories.

Your agent reads the chat, screenshots or notes and asks for songs. myradio makes and plays them.

## What you need

Choose a Mac with Apple silicon, an NVIDIA graphics card on Linux, or a DGX Spark. Here is the memory in the published runs:

- **Mac with Apple silicon.** The Mac mini run used an M4 Pro with 24 GB of memory.
- **NVIDIA graphics card on Linux.** The desktop run used an RTX 4080 with 16 GB of graphics memory on Ubuntu.
- **DGX Spark.** The run used a GB10 with 121 GB of shared memory. The music and Drama 3 voice models used 24 GB together while loaded in one server.

You also need Python 3.10 or newer and a browser.

**Disk space:** [NEEDS: measured free-disk requirement for the music model, runtime, chosen voice and saved audio; the receipts do not record disk use.]

## Connect your agent

In the studio, open **☰ Menu → Connect your agent**. Press **Copy** and paste into Claude Code, Codex or another agent that supports MCP, the connection that lets an agent use myradio’s tools.

Your agent programs the station and writes the DJ’s lines. It can read your chat or screenshots and ask for songs about them. myradio makes the songs and speaks the lines.

## Pick your DJ voice

Local voices run on your computer. Start with these, in this order:

1. **Qwen3-TTS CustomVoice.** Top local choice. It is the voice of Jessica, who already reads the news on our own live station. Apache-2.0.
2. **Drama 3 B70 Turbo.** A performed read, guided by short tone directions placed through the line. Suits big machines. Fish Audio Community License.
3. **IndexTTS2.** Use a voice sample and guide its emotion. bilibili Model Use License.

For a cloud voice, the recommendation is **ElevenLabs “Guy”**, the announcer in the launch ad. [NEEDS: ElevenLabs adapter]

**Chatterbox** is a fast local alternative under the MIT licence.

## Three runs, with receipts

Each station ran for an hour on songs made fresh during that run. All three were still playing at the end.

| Machine | On air | Aired | Music made / render time | Music speed | Failed renders |
| --- | --- | --- | --- | --- | --- |
| Mac mini · M4 Pro | 3,604 s | 25 songs | 60.6 min / 58.8 min | 1.03× | 0 of 26 |
| Linux desktop · RTX 4080 | 3,611 s | 25 songs | 68.9 min / 19.0 min | 3.64× | 0 of 28 |
| DGX Spark · GB10 | 3,610 s | 24 songs, 12 DJ clips | 61.8 min / 27.0 min | 2.29× | 0 of 40 |

A render is an attempt to make a song or spoken clip. Music speed compares the length of music made with the time spent making it. The Spark made music and DJ speech side by side; its render count includes both.

Read the receipts: [Mac mini](docs/runs/2026-11-06-mac-mini-hour.md) · [RTX 4080](docs/runs/2026-11-06-apollo-hour.md) · [DGX Spark](docs/runs/2026-11-06-toymaker-dual-loader-hour.md).

## Open source, with separate model licences

The myradio app is **Apache-2.0**. Models and the software that runs them are downloaded separately and keep their own licences.

**YuE2 songs are CC BY-NC 4.0: share them, don’t sell them.** A voice model’s licence does not change the song licence. Qwen3-TTS is Apache-2.0; Drama 3 uses the Fish Audio Community License; IndexTTS2 uses the bilibili Model Use License. ElevenLabs has its own service terms. See [LICENSE](LICENSE), [NOTICE](NOTICE) and each model’s card.

Thanks to **YuE2** for the model powering it. YuE2 is by Multimodal Art Projection.

Made by **Omnispace Technologies**, a member of the **NVIDIA Inception** program.

## Quick start

From a copy of the source, install myradio with Python:

```sh
python3 -m pip install .
```

Run the model setup command. [NEEDS: myradio setup]

```sh
myradio setup
# [NEEDS: myradio setup]
```

It detects your Mac, NVIDIA machine or DGX Spark, downloads the models and the software to run them, checks the downloads, and makes a test song. [NEEDS: myradio setup]

Open the browser studio:

```sh
python3 -m myradio.server
```

Choose your station style and DJ, then make your first songs in the setup flow. [NEEDS: myradio setup; finished first-run studio flow]

Use **Connect your agent** above to hand the show to your agent. Press **Start station**, then **Listen** to hear it in your browser.

Old sections follow: Schedule a recurring show, ID, or advertisement; Shared HTTP interface; Boundaries before a production release (limits, to be tidied); Importing your audio; Portable backup and restore; Optional real broadcast: Icecast; Verification; Music-prompt library; Unavailable audio and recovery; Private Tailscale access (retitled from the old README).
