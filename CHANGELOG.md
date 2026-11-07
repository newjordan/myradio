# Changelog

## 0.4.0 (2026-11-07)

The first public release. myradio is a radio station that runs at home on your graphics card: it makes new songs in
any genre, to your own lyrics, and your AI agent can be the DJ.

### Install

```
curl -fsSL https://myradio.garden/install.sh | sh
myradio
```

The installer works on a Mac with Apple silicon, Linux with an NVIDIA graphics card, and the NVIDIA DGX Spark. It
downloads the 4.5 GB YuE2 music model, checks every file, writes the settings and makes a test song. Windows runs the
studio with your own audio; making songs on Windows is still being tested.

### New

- `myradio setup` and `install.sh`: one command from nothing to a station that can make music.
- Songs made on your own computer with YuE2 Turbo Radio through audio.cpp, on CUDA and Metal.
- DJ voices: ElevenLabs, or local voices (Qwen3-TTS, Drama 3, IndexTTS2). The DGX Spark runs the music model and a
  DJ voice together.
- Connect your agent: one copy sets up the MCP server, so an agent can DJ the station and keep it supplied.
- The studio: one page laid out like a mixing desk, with Coming up, the library crate (music, DJ clips, spots), fade
  to next, a GO LIVE guard, and Dial, the station pet, with levels and trophies.
- Windows 10/11 support for the studio (thanks to Frank).
- Apache-2.0 licence.

### Speed

Minutes of music made per minute, over an hour on air: 3.64× on an NVIDIA RTX 4080, 3.09× on an Intel Arc Pro B70,
2.29× on an NVIDIA DGX Spark with the DJ voice, and 1.03× on an Apple Mac mini (M4 Pro). The paper is at
https://myradio.garden/research/.

### Licences

myradio is Apache-2.0. Songs made with YuE2 are CC BY-NC 4.0 (non-commercial). Each model keeps its own licence.
