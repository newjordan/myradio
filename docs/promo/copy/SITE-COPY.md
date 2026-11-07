# myradio.garden — one-page website copy

Editorial notes, destinations and media directions below are not visible page text. Bracketed NEEDS markers flag work or assets to resolve before publishing. [NEEDS: public repository URL for GitHub, README, licence and receipt links]

## Hero

**Headline:** Your life, on the radio.

**Body:** Turn your own computer into a live radio station. YuE2 makes new songs from your prompts and lyrics. Your agent, an AI assistant you connect, runs the show and writes the DJ’s words. myradio makes the music and speaks the lines.

**Tagline:** Everyone gets their own bard.

**Buttons:** “Start your station” → Run it on · “Watch the ad” → ad video.

**Image/video notes:** Place the “NOW THIS IS MyRadio” ad beside a real Console screenshot. Borrow its chrome title and spinning CD case for a little 90s TV energy. [NEEDS: approved ad embed; selected screenshot]

**Hero headline alternatives (editorial):**

- “Your life, on the radio.” — Recommended. It leads with the personal examples and leaves the body to explain the app.
- “Your computer. Your radio station.”
- “Everyone gets their own bard.”

## Examples

**Headline:** Start with something that happened to you.

**Body:**

**Frank’s game night, sung live.** An agent grabbed screenshots of Frank’s Dota stream and asked the station for songs as he played. Out came “Riki with a Dagon”, “Boots of Travel” and “They Can’t Doubt the Hands”.

**Your chat, sung live.** Your agent reads Twitch chat. myradio makes songs from the shout-outs and inside jokes it sends over.

**Your notes, in a song.** Give your agent work or study notes. Ask for a song about what you’re learning.

**Your family, in the lyrics.** Make a party personal with dedications and songs about shared memories.

**Button:** “Make a station of your own” → Run it on.

**Image/video notes:** Lead with Frank’s game footage beside the matching song. Follow with chat beside lyrics, notes beside their song, and a family dedication card. Use the real example songs.

## How it works

**Headline:** Give it a subject. Make it a station.

**Step 1 — Set up your music maker.** Run `myradio setup` to download the models and link them to your station. [NEEDS: myradio setup]

**Step 2 — Connect your agent.** In the studio, open “Connect your agent”, press “Copy” and paste into Claude Code, Codex or another agent with MCP. MCP lets an agent use myradio’s tools. Give your agent a style, a subject or some lyrics.

**Step 3 — Put it on air.** Your agent asks for songs and writes the DJ lines. myradio makes the audio. Press “Start station”, then “Listen” in your browser.

**Buttons:** “Read the quick start” → README Quick start · “Connect your agent” → README Connect your agent.

**Image/video notes:** Show the setup command, the real Copy panel and a song playing in the Console. [NEEDS: myradio setup; finished first-run footage for the setup shot]

## Your DJ

**Headline:** Your agent has the words. Pick the voice.

**Body:** Connect your favourite agent to program the station and write the breaks between songs. myradio’s voice reads those lines.

Local choices, in our recommended order:

1. **Qwen3-TTS CustomVoice.** Our top local choice. It is the voice of Jessica, who already reads the news on our own live station. Apache-2.0.
2. **Drama 3 B70 Turbo.** Place short tone directions through the line to guide a performed read. A choice for big machines. Fish Audio Community License.
3. **IndexTTS2.** Use a voice sample and guide its emotion. bilibili Model Use License.

Prefer a cloud voice? **ElevenLabs “Guy”** is the announcer you hear in the ad. [NEEDS: ElevenLabs adapter]

Chatterbox is another fast local option, under the MIT licence.

**Button:** “Connect your agent” → README Connect your agent.

**Image/video notes:** Show the agent writing a break, then the speech clip on air. Use a real high-energy DJ break made on the DGX Spark (the ad's "Late Epoch" station ID works), with captions.

## Run it on

**Headline:** Your computer is the station.

**Body:** Run myradio on a Mac with Apple silicon, a Linux computer with an NVIDIA graphics card, or a DGX Spark.

Memory in the published runs:

- **Mac with Apple silicon.** The Mac mini run used an M4 Pro with 24 GB of memory.
- **NVIDIA graphics card on Linux.** The desktop run used an RTX 4080 with 16 GB of graphics memory on Ubuntu.
- **DGX Spark.** The run used a GB10 with 121 GB of shared memory. The music and Drama 3 voice models used 24 GB together while loaded in one server.

For the source install, you also need Python 3.10 or newer and a browser.

**Disk space:** [NEEDS: measured free-disk requirement for the music model, runtime, chosen voice and saved audio; the receipts do not record disk use.]

**Setup copy:** Run `myradio setup` to detect your machine, download the models and the software that runs them, check the downloads and make a test song. [NEEDS: myradio setup]

**Buttons:** “Read the quick start” → README Quick start · “Get the source” → GitHub.

**Image/video notes:** Show a Mac mini, an NVIDIA desktop and a DGX Spark beside their real station screens. Label the images by machine kind.

## Receipts

**Headline:** Three computers. Three hours on air.

**Body:** These are the published runs from 2026-11-06. Every song that aired was made during its run. Each station was still playing at the end.

| Machine | On air | Aired | Music made / render time | Music speed | Failed renders |
| --- | --- | --- | --- | --- | --- |
| Mac mini · M4 Pro | 3,604 s | 25 songs | 60.6 min / 58.8 min | 1.03× | 0 of 26 |
| Linux desktop · RTX 4080 | 3,611 s | 25 songs | 68.9 min / 19.0 min | 3.64× | 0 of 28 |
| DGX Spark · GB10 | 3,610 s | 24 songs, 12 DJ clips | 61.8 min / 27.0 min | 2.29× | 0 of 40 |

A render is an attempt to make a song or spoken clip. Music speed compares the length of music made with the time spent making it. The Spark made music and speech side by side. Its render count includes both.

**Buttons:** “Mac mini receipt” · “RTX 4080 receipt” · “DGX Spark receipt” → respective published run pages.

**Image/video notes:** Pair the table with real on-air clips: Skatepark Radio on the Mac mini, Late Epoch on the RTX 4080, and Late Epoch on the Spark, with its DJ breaks.

## Open source

**Headline:** The station is yours to build on.

**Body:** myradio is open source under Apache-2.0. Read the code, change it and share your changes under the licence.

The models and the software that runs them keep their own licences. **YuE2 songs are CC BY-NC 4.0: share them, don’t sell them.** Voice licences are separate from the song licence. ElevenLabs has its own service terms.

Thanks to YuE2 for the model powering it. YuE2 is by Multimodal Art Projection.

Want to help? Join the research into faster, better music and voice models.

**Buttons:** “View on GitHub” · “Read the licences” · “Help with research” → repository, NOTICE and research page.

**Image/video notes:** Keep this section text-led. Link the music model card beside the YuE2 credit.

## FAQ

**Headline:** Before you take the mic.

**1. Is this a playlist of existing songs?**

YuE2 makes new songs from your prompts and lyrics. In the published runs, every song that aired was made during that run.

**2. Who reads my chat and writes the DJ’s words?**

Your agent does. It reads the chat or screenshots, asks for songs and writes the spoken lines. myradio makes the music and speaks those lines.

**3. What computer do I need?**

A Mac with Apple silicon, a Linux computer with an NVIDIA graphics card, or a DGX Spark. See the machines and memory in the published runs above.

**4. Does myradio.garden host my station?**

The website is the home for the project. Your computer runs your station and its private browser studio.

**5. Can the DJ voice run on my computer?**

Yes. Qwen3-TTS CustomVoice, Drama 3 and IndexTTS2 are local voice choices. ElevenLabs “Guy” is the recommended cloud option. [NEEDS: ElevenLabs adapter]

**6. Can I use it with Twitch?**

Your agent can read Twitch chat and ask myradio for songs about it. Direct output to Twitch or YouTube needs a video-stream connection called RTMP. [NEEDS: RTMP output]

**7. Can I share or sell the songs?**

YuE2 songs are CC BY-NC 4.0: share them, don’t sell them. The app’s Apache-2.0 licence and each voice model’s licence are separate.

**8. How do I get started?**

Install from the source, then run `myradio setup` to download and connect the models. [NEEDS: myradio setup]

Connect your agent in the studio. Press “Start station”, then “Listen” in your browser.

**Button:** “Read the quick start” → README Quick start.

**Image/video notes:** None.

## Footer

**Headline:** Everyone gets their own bard.

**Body:** Made by Omnispace Technologies, a member of the NVIDIA Inception program.

Thanks to YuE2 for the model powering it.

App: Apache-2.0. YuE2 songs: CC BY-NC 4.0. Share them, don’t sell them.

**Buttons / links:** “Start your station” → Run it on · “GitHub” · “Music model” · “Licences” · “Research”.

**Image/video notes:** Use the Omnispace Technologies logo and official NVIDIA Inception Program member badge. [NEEDS: official company logo and member badge assets]
