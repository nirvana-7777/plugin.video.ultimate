# plugin.video.ultimate

A Kodi video addon that connects to an **Ultimate Backend** server and exposes all providers, channels, live events, VOD content, favorites, and recordings for playback inside Kodi.

---

## Features

| Feature                | Details                                                            |
|------------------------|--------------------------------------------------------------------|
| Providers              | All enabled providers on the server are listed automatically       |
| Channels               | Live channel browsing with logo/EPG info, Catch-up support         |
| Events                 | Sports / PPV events with start time display and LIVE status        |
| Live Now               | Filtered view showing only currently live events                   |
| VOD                    | Full tree-navigation for VOD categories and items                  |
| VOD Search             | Search across all VOD content                                      |
| Favorites              | View and play favorited channels and VOD items                     |
| Recordings             | Browse and play back recorded content with resume support          |
| DRM                    | Widevine & PlayReady via InputStream Adaptive (Kodi 21/22 support) |
| Server-side decryption | Optional – no Widevine CDM required                                |
| M3U Export             | Export channels to M3U playlist for use with other players         |
| VOD Library Export     | Export VOD categories to Kodi library                              |
| Configurable server    | IP/hostname and port set in addon settings                         |
| Auto retry             | Waits for backend to become available on first launch              |

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

| Setting                    | Description                                     | Default     |
|----------------------------|-------------------------------------------------|-------------|
| Server IP / Hostname       | IP or hostname of the Ultimate Backend server   | `localhost` |
| Server Port                | TCP port                                        | `8000`      |
| Use HTTPS                  | Enable TLS                                      | `false`     |
| Use InputStream Adaptive   | Required for DRM and MPEG-DASH                  | `true`      |
| Preferred DRM              | widevine / playready / auto                     | `auto`      |
| Show provider in title     | Prefix channel names with `[provider]`          | `true`      |

---

## Playback modes

### Mode 1 – Client-side DRM (default)
The addon fetches the raw MPEG-DASH manifest and the DRM license URL from the backend, then passes both to `inputstream.adaptive`. Widevine or PlayReady is used to decrypt in-player. **Requires Widevine CDM** (install via `script.module.inputstreamhelper`).

---

## DRM Support Details

The addon supports multiple Kodi versions with appropriate DRM configuration:

| Kodi Version      | DRM Method                         | Notes                                                                          |
|-------------------|------------------------------------|--------------------------------------------------------------------------------|
| Kodi 22+          | `inputstream.adaptive.drm` (JSON)  | Full feature support including wrapper/unwrapper, req_data, server_certificate |
| Kodi 21 (simple)  | `inputstream.adaptive.drm_legacy`  | Pipe format: `KeySystem\|LicenseURL\|Headers`                                  |
| Kodi 21 (complex) | `inputstream.adaptive.license_key` | Used when wrapper/unwrapper/req_data are present                               |
| Kodi 19-20        | `inputstream.adaptive.license_key` | Legacy format with post_data and response_data                                 |

### DRM Header Support
The addon reads `x-kodi-drm-configs` and `x-kodi-stream-headers` from manifest responses (including redirects), falling back to the `/drm` endpoint when headers are absent.

---

## URL scheme
