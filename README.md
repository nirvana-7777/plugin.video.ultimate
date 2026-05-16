# plugin.video.ultimate

A Kodi video addon that connects to an **Ultimate Backend** server and exposes all providers, channels, live events and VOD content for playback inside Kodi.

---

## Features

| Feature | Details |
|---|---|
| Providers | All enabled providers on the server are listed automatically |
| Channels | Live channel browsing with logo/EPG info |
| Events | Sports / PPV events with start time display |
| VOD | Full tree-navigation for VOD categories and items |
| DRM | Widevine & PlayReady via InputStream Adaptive |
| Server-side decryption | Optional – no Widevine CDM required |
| Configurable server | IP/hostname and port set in addon settings |

---

## Requirements

- Kodi 19 (Matrix) or later
- **inputstream.adaptive** addon (pre-installed on most platforms)
- **script.module.inputstreamhelper** (optional, for automatic Widevine install)
- An Ultimate Backend server reachable from Kodi

---

## Installation

1. Copy the folder `plugin.video.ultimate/` into your Kodi addons directory:
   - Linux: `~/.kodi/addons/`
   - Windows: `%APPDATA%\Kodi\addons\`
   - Android: `/sdcard/Android/data/org.xbmc.kodi/files/.kodi/addons/`
2. In Kodi, go to **Settings → Add-ons → My add-ons → Video add-ons** and enable **Ultimate Backend**.
3. Open addon settings and configure your server IP and port.

---

## Settings

| Setting | Description | Default |
|---|---|---|
| Server IP / Hostname | IP or hostname of the Ultimate Backend server | `localhost` |
| Server Port | TCP port | `8000` |
| Use HTTPS | Enable TLS | `false` |
| Use InputStream Adaptive | Required for DRM and MPEG-DASH | `true` |
| Use server-side decryption | Let the server decrypt — no Widevine CDM needed | `false` |
| Preferred DRM | widevine / playready / auto | `auto` |
| Show provider in title | Prefix channel names with `[provider]` | `true` |

---

## Playback modes

### Mode 1 – Client-side DRM (default)
The addon fetches the raw MPEG-DASH manifest and the DRM license URL from the backend, then passes both to `inputstream.adaptive`. Widevine or PlayReady is used to decrypt in-player. **Requires Widevine CDM** (install via `script.module.inputstreamhelper`).

### Mode 2 – Server-side decryption
Enable *"Use server-side decryption"* in settings. The backend decrypts the stream before delivery — Kodi receives a clear MPEG-DASH stream that `inputstream.adaptive` can play without any DRM CDM. Use this if you cannot install Widevine (e.g. LibreELEC on ARM).

---

## URL scheme

```
plugin://plugin.video.ultimate/                           # root (provider list)
plugin://plugin.video.ultimate/?action=provider_menu&provider=<name>
plugin://plugin.video.ultimate/?action=channels&provider=<name>
plugin://plugin.video.ultimate/?action=events&provider=<name>
plugin://plugin.video.ultimate/?action=vod&provider=<name>
plugin://plugin.video.ultimate/?action=vod_path&provider=<name>&path=<slug/path>
plugin://plugin.video.ultimate/?action=play_channel&provider=<name>&channel_id=<id>
plugin://plugin.video.ultimate/?action=play_event&provider=<name>&event_id=<id>
plugin://plugin.video.ultimate/?action=play_vod&provider=<name>&vod_path=<slug/path>
```

---

## API endpoints used

| Endpoint | Purpose |
|---|---|
| `GET /api/providers` | List all enabled providers |
| `GET /api/providers/{p}/channels` | Channel list |
| `GET /api/providers/{p}/channels/{id}/stream/index.mpd` | Live stream |
| `GET /api/providers/{p}/channels/{id}/drm` | DRM configs |
| `GET /api/providers/{p}/events` | Events list |
| `GET /api/providers/{p}/events/{id}/stream/index.mpd` | Event stream |
| `GET /api/providers/{p}/events/{id}/drm` | Event DRM |
| `GET /api/providers/{p}/vod` | VOD root |
| `GET /api/providers/{p}/vod/{path}` | VOD sub-path |
| `GET /api/providers/{p}/vod/{path}/stream/index.mpd` | VOD stream |
| `GET /api/providers/{p}/vod/{path}/drm` | VOD DRM |
| `…/stream/decrypted/index.mpd` | Server-side decrypted variants |
