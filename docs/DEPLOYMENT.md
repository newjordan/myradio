# myradio.garden and Knightshift: deployment boundaries

The operator owns **myradio.garden** and wants it to advertise and host myradio.
Knightshift is the intended later real station deployment after readiness gates
are met. This document records intent, not an assertion of configured DNS,
certificates, a live website, or a hosting purchase. None were changed here.

## Separate three surfaces

1. **Public product site** — myradio.garden: introduction, honest capability
   list, screenshots, requirements, installation/downloads once built, privacy
   and operating guidance. Static hosting is sufficient. No launch codes, AI
   keys, Icecast source passwords, or control APIs belong here.
2. **Public listening** — listener-facing player/site and a TLS listener URL
   served by an Icecast host. A public listener must not need the studio token.
   Domain, hosting, bandwidth limits, rights, mount access and listener capacity
   must be chosen and independently tested. Never embed the authenticated local
   `/stream.wav` monitor URL/token into a public site. Current browser monitor
   is private, not the public distribution endpoint.
3. **Private studio** — the station engine runs on the user's rig/server. Its
   dashboard/API remain loopback by default. An explicitly configured private
   HTTPS reverse proxy may reach it using `--public-origin` and authentication.
   That flag is not account management, tenancy, per-user isolation, or a license
   to expose the current development service on the open internet.

Public marketing and self-hosting do **not** imply a secure multi-tenant hosted
station service exists. If hosted accounts are wanted, provisioning, isolation,
quotas, user authentication, recovery, billing, monitoring and abuse handling
need implementation and review first. No route should proxy arbitrary station
controls to anonymous public visitors.

## Before publishing

- Confirm domain/DNS authority, hosting destination, and whether initial hosting
  means static promotion + downloads or managed per-user stations.
- Build and verify distributable installers for the supported OSes. The source
  launcher/old wheel is not a signed native one-click installer. Keep current
  source screenshots clearly separate from older downloadable builds.
- Publish only honest claims: local playout, library import, scheduling,
  approval-gated optional AI, and separately configured Icecast output work;
  music/voice generation, bundled inference, and production service lifecycle
  are not complete. No invented users, testimonials, listener counts or uptime.
- Check download integrity, installation/uninstall paths, backups, restore,
  upgrade/rollback, logs and unattended recovery. Conduct real listener decode
  and long-running tests on the intended broadcast host.
- Keep Knightshift deployment configuration and credentials outside public
  assets and portable backups. Independent production acceptance is still open.

## Preview without Tailscale

Actual source screenshots are saved under `docs/screenshots/midnight-*.png` and
can be opened directly on Stage. The source can be started with
`python3 -m myradio.server` on the machine running it. A link to 127.0.0.1 works
only on that machine; this does not solve remote reachability. The earlier
Tailscale preview is not assumed reachable or current. No public Funnel or DNS
change was made as a workaround.
