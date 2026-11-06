# Midnight FM — concept 01

A rendered HTML/CSS interface concept, not the shipped dashboard. `midnight-fm.png` is a Chromium render of `midnight-fm.html`, at 1600 CSS pixels wide (1.5× raster scale). Track names, art, waveform, meter and conversation are illustrative. No image-generation service was available; this is a deliberately composed interface image with editable source.

## Experience direction

Make the station feel like an instrument, not an administration form. Ink-blue desk, warm coral action color, soft lime operator/approval accents, large original geometric record artwork, quiet typography, and a compact persistent navigation rail. Avoid making every panel compete for attention.

Studio should answer: What is playing? What happens next? Who can hear it? What can I ask the operator to do? The station's mood comes from its library and identity; do not imply audio or speech generation is already available.

## Interaction contract for the rebuild

- First run: import audio → start private playout → listen. Keep this primary path visible without needing AI, provider keys, or Icecast.
- Private playout and external broadcast always have separate, text-labelled states. Go live opens a destination/rights review, not an instant hidden broadcast.
- Stop is stop, not pause. Use a square icon and the word Stop; preserve actual transport semantics.
- Operator chat stays beside the desk on desktop. On mobile it becomes a reachable labelled tab/sheet, not a crushed second column. Preserve drafts on view changes.
- Library, schedules, station identity and broadcast setup move out of the main desk, retaining the existing IDs/API functionality during migration. Use real views and keyboard-operable controls, not decorative nav items.
- Approval cards describe exactly what the existing operations will do. The illustrated positional insertion after a particular song needs an actual queue-placement operation before it can be offered; current queue.add only appends. Do not ship a misleading card.
- Do not ship the sample artwork as a claim that album art is retrieved. Generated artwork may be a local deterministic fallback; uploaded track data remains authoritative.
- Waveforms and meters must reflect measured audio or be clearly decorative. Never animate fake signal, audience counts, connection health or activity as evidence of a live station.
- Long titles, large libraries, empty states, unavailable audio, disconnected providers and latched storage faults need first-class states. Errors stay visible, not under a new navigation view.
- No drag-only reordering. Keyboard controls, focus indication, text labels, reduced motion, contrast, and mobile layouts are release gates for the rebuild.

## Implementation sequence

1. Review this concept's visual direction with the operator.
2. Rebuild the actual dashboard shell and navigation while preserving working operations.
3. Implement studio/queue/operator components, empty onboarding and failure states.
4. Port configuration forms into dedicated views, with responsive navigation.
5. Run actual browser workflows on the rebuilt app, including approvals, live audio and mobile keyboard/focus checks. Concept renders are not functional acceptance.
