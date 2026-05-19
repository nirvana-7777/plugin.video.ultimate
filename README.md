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
| **VOD Library Export** | **Export VOD categories to Kodi library for native browsing**      |
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

| Setting                     | Description                                   | Default                   |
|-----------------------------|-----------------------------------------------|---------------------------|
| Server IP / Hostname        | IP or hostname of the Ultimate Backend server | `localhost`               |
| Server Port                 | TCP port                                      | `8000`                    |
| Use HTTPS                   | Enable TLS                                    | `false`                   |
| Use InputStream Adaptive    | Required for DRM and MPEG-DASH                | `true`                    |
| Show provider in title      | Prefix channel names with `[provider]`        | `true`                    |
| Export root path            | Folder where exported files are saved         | `special://home/ultimate` |
| Include posters and artwork | Download images when exporting to library     | `false`                   |

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

## Exporting VOD to Kodi Library

You can export VOD categories (e.g., "Filme", "Serien") directly into your Kodi video library. This lets you browse your VOD content like any other local media, with posters, descriptions, and proper metadata.

### Step 1: Export from the Addon

1. Open the **Ultimate** addon in Kodi
2. Navigate to your **provider** (e.g., "RTL+")
3. Go into the **VOD** section
4. Browse to the folder you want to export (e.g., **Filme** or a specific series)
5. **Right-click** or **long-press** (depending on your device – try "C" on keyboard, context menu button on remote, or long-press on touchscreens)
6. Select **Export to Library** from the context menu

A progress dialog will appear. Once complete, the content is exported to:
`special://home/ultimate/library/<provider>/vod/<folder_name>/`

> **Tip:** You can change the export location in Addon Settings → Export → Export root path.

### Step 2: Import into Kodi Library

Now you need to tell Kodi to add this folder to your video library:

1. Go to Kodi **Settings** (gear icon)
2. Select **Media** → **Library** → **Videos** → **Add Videos...**
3. Click **Browse**
4. Navigate to: `special://home/ultimate/library/<provider>/vod/`
   - On most systems, you can type `special://home/ultimate/` directly into the path field
5. Select the folder you exported (e.g., **Filme**)
6. Click **OK**
7. Choose the **content type** that matches your export:
   - **Movies** – for movie folders (e.g., "Filme")
   - **TV Shows** – for series folders (e.g., "Serien")
8. Select a **scraper** (e.g., "The Movie Database" for movies, "TVDB" for series)
9. Click **OK** and confirm **Yes** when asked to refresh the library

Your exported VOD content will now appear in Kodi's main video library alongside your other media!

### Updating Exported Content

The export does **not** automatically sync when new content is added to the server. To update:

- Re-export the same folder (the addon will clean up stale files and add new ones)
- Then go to **Settings → Media → Library → Videos → Update library** (or right-click the source and choose "Scan for new content")

### Changing the Export Location

1. Open **Addon Settings** (right-click the addon → Settings)
2. Go to the **Export** category
3. Change **Export root path** to your preferred location (e.g., an external drive)
4. Re-export any folders you want moved

---

## URL scheme