#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
m3u_export.py – Channel-to-M3U export for Ultimate Backend addon.

Exports all channels of a provider to an M3U playlist file.
"""

import os
import threading
import re
from urllib.parse import urlencode

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from api import UltimateBackendClient, APIError
from utils import get_setting, get_setting_bool, notify, notify_error, safe_image_url

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

CONTEXT_MENU_LABEL = "Export Channels to M3U"

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_ADDON = xbmcaddon.Addon()
_LOG_TAG = "[Ultimate/M3UExport]"


def _log(msg, level=xbmc.LOGDEBUG):
    xbmc.log(f"{_LOG_TAG} {msg}", level)


def _resolve_export_root():
    # type: () -> str
    """
    Return the absolute export root path from the addon setting, or a safe
    default inside Kodi's own userdata directory.

    Always returns a plain filesystem path (special:// URIs are translated
    by xbmcvfs.translatePath before being returned).
    """
    from_setting = get_setting("export_root_path") or ""
    if from_setting:
        return xbmcvfs.translatePath(from_setting.rstrip("/\\"))
    default = "special://profile/addon_data/plugin.video.ultimate/library"
    return xbmcvfs.translatePath(default)


def _sanitise(name):
    # type: (str) -> str
    """Replace filesystem-unsafe characters with an underscore."""
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def _write_m3u_file(filepath, channels, provider, progress=None):
    # type: (str, list, str, object) -> int
    """
    Write channels to an M3U file using xbmcvfs for full Kodi path support.

    M3U format reference:
      #EXTM3U
      #EXTINF:-1 tvg-logo="<url>" tvg-name="<name>" group-title="<group>",<Display Name>
      plugin://...

    Attributes on the #EXTINF line are space-separated; only the comma
    immediately before the display name is significant.  Duration is always
    -1 for live/unknown-length streams (both TV and radio).

    Returns the number of channel entries written.
    """
    lines = ["#EXTM3U"]
    total = len(channels)

    for idx, ch in enumerate(channels):
        ch_id = ch.get("Id", "")
        name  = ch.get("Name", ch_id)
        logo  = safe_image_url(ch.get("LogoUrl", ""))

        # Build the plugin:// URL for this channel
        params = urlencode({
            "action":       "play_channel",
            "provider":     provider,
            "channel_id":   ch_id,
            "channel_name": name,
        })
        stream_url = f"plugin://plugin.video.ultimate/?{params}"

        # Build #EXTINF attribute string — attributes are space-separated,
        # then a single comma, then the display title.
        attrs = []
        if logo:
            attrs.append(f'tvg-logo="{logo}"')
        attrs.append(f'tvg-name="{name}"')
        attrs.append(f'group-title="{provider}"')

        extinf_line = f"#EXTINF:-1 {' '.join(attrs)},{name}"
        lines.append(extinf_line)
        lines.append(stream_url)

        # Update progress every 50 channels so the dialog stays responsive
        # without hammering the UI on every single channel.
        if progress is not None and idx % 50 == 0:
            pct = 50 + int((idx / total) * 45)   # 50 % → 95 %
            progress.update(pct, message=f"Writing channel {idx + 1}/{total}…")

    content  = "\n".join(lines)
    tmp_path = filepath + ".tmp"

    # Use xbmcvfs so the write works on any Kodi-supported path (network
    # shares, SMB, etc.) not just plain local filesystem paths.
    f = xbmcvfs.File(tmp_path, "w")
    try:
        if not f.write(content):
            raise IOError(f"xbmcvfs.File.write() returned False for {tmp_path}")
    finally:
        f.close()

    # Atomic rename — xbmcvfs.rename is the Kodi-safe equivalent of os.replace
    if not xbmcvfs.rename(tmp_path, filepath):
        # Fall back to delete+rename on platforms where rename-over-existing
        # is not supported (e.g. some Windows SMB mounts).
        xbmcvfs.delete(filepath)
        if not xbmcvfs.rename(tmp_path, filepath):
            raise IOError(f"Could not rename {tmp_path} → {filepath}")

    return total


def _get_client():
    # type: () -> UltimateBackendClient
    """Create a backend client from addon settings.

    m3u_export runs in a background thread spawned *after* the main plugin
    process has already exited, so it cannot reference addon.get_client().
    A fresh client is cheap and correct here.
    """
    ip       = get_setting("server_ip") or "localhost"
    raw_port = get_setting("server_port") or "8000"
    try:
        port = int(raw_port)
    except (ValueError, TypeError):
        port = 8000
    use_https = get_setting_bool("use_https")
    return UltimateBackendClient(ip, port, use_https=use_https)


def export_channels_m3u(provider):
    # type: (str) -> None
    """
    Export all channels of a provider to an M3U file.

    Runs entirely in a daemon background thread so it never blocks Kodi's UI.
    The output file is:
        <export_root>/m3u/<provider_safe>/channels.m3u

    Parameters
    ----------
    provider : provider name (e.g. "rtlplus")
    """
    if not provider:
        notify_error("Export: missing provider.")
        return

    export_root  = _resolve_export_root()
    provider_safe = _sanitise(provider)

    # Target: <export_root>/m3u/<provider>/channels.m3u
    target_dir  = os.path.join(export_root, "m3u", provider_safe)
    target_file = os.path.join(target_dir, "channels.m3u")

    _log(f"export_channels_m3u: provider={provider} target={target_file}", xbmc.LOGINFO)

    def _run():
        monitor  = xbmc.Monitor()
        progress = xbmcgui.DialogProgressBG()
        progress.create(
            "Ultimate – Export Channels",
            f"Fetching channels for '{provider}'…",
        )

        try:
            if monitor.abortRequested():
                return

            client = _get_client()

            # Fetch channels
            progress.update(10, message="Loading channels from server…")
            channels = client.get_channels(provider)

            if not channels:
                notify(f"No channels found for provider '{provider}'.")
                return

            # Create directory — xbmcvfs.mkdirs handles special:// paths and
            # creates intermediate directories in one call.
            progress.update(30, message="Creating export directory…")
            if not xbmcvfs.mkdirs(target_dir):
                # mkdirs returns False if the directory already exists on some
                # Kodi builds; only treat it as a real error when the dir is
                # genuinely absent afterwards.
                if not xbmcvfs.exists(target_dir):
                    raise IOError(f"Could not create directory: {target_dir}")

            # Write M3U (progress is updated inside _write_m3u_file)
            progress.update(50, message=f"Writing {len(channels)} channels to M3U…")
            written = _write_m3u_file(target_file, channels, provider, progress)

            # Done
            progress.update(100, message="Done.")
            notify(
                f"Exported {written} channels to:\n{target_file}",
                icon=xbmcgui.NOTIFICATION_INFO,
                time=5000,
            )
            _log(f"Export complete: {written} channels → {target_file}", xbmc.LOGINFO)

        except APIError as e:
            _log(f"API error: {e}", xbmc.LOGERROR)
            notify_error(f"Export failed (API error): {e}")
        except Exception as e:
            _log(f"Unexpected error: {e}", xbmc.LOGERROR)
            notify_error(f"Export failed unexpectedly: {e}")
        finally:
            progress.close()

    thread = threading.Thread(target=_run, name="m3u_export", daemon=True)
    thread.start()