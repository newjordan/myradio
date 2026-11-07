---
description: Check or steer your myradio station (what's on air, skip, queue a song)
argument-hint: "[what you want, e.g. skip this one]"
---
Use the myradio tools (mcp__myradio_*) for my radio station. First call station_status.
If I asked for something, do it: $ARGUMENTS
Otherwise tell me in three short lines what is on air, what is queued next and whether the stream is live.
Don't call broadcast_start, broadcast_stop or library_delete unless I said so in this message.
