#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
m3u_export.py – Channel-to-M3U export for Ultimate Backend addon.

Exports all channels of a provider to an M3U playlist file.
"""

from urllib.parse import urlencode

import os
import xbmc
import xbmcgui

from api import APIError
from utils import (
    EXPORT_TYPE_PLAYLISTS,
    atomic_write,
    ensure_export_path,
    get_client_from_settings,
    notify,
    notify_error,
    run_in_background,
    safe_image_url,
    ExportProgress,
)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

CONTEXT_MENU_LABEL = "Export Channels to M3U"
_LOG_TAG = "[Ultimate/M3UExport]"


def _log(msg, level=xbmc.LOGDEBUG):
    xbmc.log(f"{_LOG_TAG} {msg}", level)


def _build_extinf_line(channel, provider):
    """
    Build the #EXTINF line for a channel.

    Format: #EXTINF:-1 tvg-logo="url" tvg-name="name" group-title="group",Display Name
    """
    ch_id = channel.get("Id", "")
    name = channel.get("Name", ch_id)
    logo = safe_image_url(channel.get("LogoUrl", ""))

    attrs = []
    if logo:
        attrs.append(f'tvg-logo="{logo}"')
    attrs.append(f'tvg-name="{name}"')
    attrs.append(f'group-title="{provider}"')

    return f"#EXTINF:-1 {' '.join(attrs)},{name}"


def _build_channel_url(provider, channel_id, channel_name):
    """Build the plugin:// playback URL for a channel."""
    params = urlencode({
        "action": "play_channel",
        "provider": provider,
        "channel_id": channel_id,
        "channel_name": channel_name,
    })
    return f"plugin://plugin.video.ultimate/?{params}"


def _write_m3u_content(filepath, channels, provider, progress):
    """
    Write channels to an M3U file.

    Returns the number of channel entries written.
    """
    lines = ["#EXTM3U"]
    total = len(channels)

    for idx, ch in enumerate(channels):
        ch_id = ch.get("Id", "")
        name = ch.get("Name", ch_id)

        extinf_line = _build_extinf_line(ch, provider)
        stream_url = _build_channel_url(provider, ch_id, name)

        lines.append(extinf_line)
        lines.append(stream_url)

        # Update progress every 50 channels
        if idx % 50 == 0:
            pct = int((idx / total) * 100)
            progress.update(pct, message=f"Writing channel {idx + 1}/{total}…")

    content = "\n".join(lines)
    atomic_write(filepath, content)
    return total


def _run_export(provider):
    """Background thread function for M3U export."""
    progress = ExportProgress(
        "Ultimate – Export Channels",
        f"Fetching channels for '{provider}'…"
    )

    try:
        if progress.is_aborted():
            return

        # Get client and fetch channels
        client = get_client_from_settings()
        channels = client.get_channels(provider)

        if not channels:
            notify(f"No channels found for provider '{provider}'.")
            return

        # Determine target file path
        export_dir = ensure_export_path(EXPORT_TYPE_PLAYLISTS, provider)
        target_file = os.path.join(export_dir, "channels.m3u")
        _log(f"Export target: {target_file}", xbmc.LOGINFO)

        # Write M3U file
        progress.update(10, message=f"Found {len(channels)} channels. Writing…")
        written = _write_m3u_content(target_file, channels, provider, progress)

        # Done
        progress.update(100, message="Done.")
        _log(f"Export complete: {written} channels → {target_file}", xbmc.LOGINFO)
        notify(
            f"Exported {written} channels to:\n{target_file}",
            icon=xbmcgui.NOTIFICATION_INFO,
            time=5000,
        )

    except APIError as e:
        _log(f"API error: {e}", xbmc.LOGERROR)
        notify_error(f"Export failed (API error): {e}")
    except Exception as e:
        _log(f"Unexpected error: {e}", xbmc.LOGERROR)
        notify_error(f"Export failed unexpectedly: {e}")
    finally:
        progress.close()


def export_channels_m3u(provider):
    """
    Export all channels of a provider to an M3U file.

    Runs in a background thread so it never blocks Kodi's UI.

    Output file: <export_root>/playlists/<provider_safe>/channels.m3u

    Parameters
    ----------
    provider : provider name (e.g. "rtlplus")
    """
    if not provider:
        notify_error("Export: missing provider.")
        return

    run_in_background(_run_export, "m3u_export", provider)