#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ultimate Backend – Kodi Video Addon
Entry point: routes plugin:// URLs to appropriate handlers.
"""

import sys
import os
import json
import time
from typing import Optional, Tuple

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ADDON_DIR, "resources", "lib"))

from urllib.parse import parse_qsl, urlencode, quote
from base64 import b64decode

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

from resources.lib.api import UltimateBackendClient, APIError
from resources.lib.drm_helper import check_inputstream_adaptive, ensure_widevine
from resources.lib.m3u_export import export_channels_m3u, CONTEXT_MENU_LABEL as M3U_CONTEXT_LABEL
from resources.lib.vod_export import export_vod_library, CONTEXT_MENU_LABEL
from resources.lib.utils import (
    get_setting, get_setting_bool,
    notify, notify_error,
    safe_image_url, provider_label,
)

ADDON    = xbmcaddon.Addon()
HANDLE   = int(sys.argv[1])
BASE_URL = sys.argv[0]
PARAMS   = dict(parse_qsl(sys.argv[2].lstrip("?")))

# Module-level client cache (re-used within the same plugin invocation).
# Each plugin:// URL is a fresh process in Kodi's execution model, so this
# cache is safe — it will never bleed across separate user navigations.
_client = None  # type: Optional[UltimateBackendClient]


def get_client():
    # type: () -> UltimateBackendClient
    global _client
    if _client is None:
        ip       = get_setting("server_ip") or "localhost"
        raw_port = get_setting("server_port") or "8000"
        try:
            port = int(raw_port)
        except (ValueError, TypeError):
            xbmc.log(
                f"[Ultimate] Invalid server_port setting '{raw_port}', using 8000",
                xbmc.LOGWARNING,
            )
            port = 8000
        use_https = get_setting_bool("use_https")
        _client = UltimateBackendClient(ip, port, use_https=use_https)
    return _client


def _wait_for_backend(client):
    # type: (UltimateBackendClient) -> bool
    """Try to reach the backend with exponential back-off.

    Attempts: immediate, 1s, 2s, 4s, 8s, 16s — then gives up.
    Shows a background progress dialog so the user can see what is happening.
    Returns True when the backend is reachable, False after all retries fail.
    """
    # Delays between attempts: 0 = try immediately, then 1, 2, 4, 8, 16 s.
    DELAYS    = [0, 1, 2, 4, 8, 16]
    total_max = sum(DELAYS)   # 31 s worst-case — used for progress bar scaling

    dialog = xbmcgui.DialogProgressBG()
    dialog.create("[Ultimate]", "Connecting to backend\u2026")

    elapsed_so_far = 0

    try:
        for attempt, delay in enumerate(DELAYS, start=1):
            if delay:
                xbmc.log(
                    f"[Ultimate] Backend not ready, retrying in {delay}s "
                    f"(attempt {attempt}/{len(DELAYS)})\u2026",
                    xbmc.LOGDEBUG,
                )

                # Sleep in small steps so we can honour an abort request.
                monitor   = xbmc.Monitor()
                slept     = 0.0
                step      = 0.25
                while slept < delay:
                    if monitor.abortRequested():
                        xbmc.log(
                            "[Ultimate] Abort requested while waiting for backend.",
                            xbmc.LOGWARNING,
                        )
                        return False
                    time.sleep(min(step, delay - slept))
                    slept += step
                    pct = min(int((elapsed_so_far + slept) / total_max * 99), 99)
                    dialog.update(
                        pct,
                        message=f"Backend not ready, retrying in {int(delay - slept + 1)}s\u2026",
                    )

                elapsed_so_far += delay

            # --- attempt the connection ---
            dialog.update(
                min(int(elapsed_so_far / total_max * 99), 99),
                message=f"Connecting\u2026 (attempt {attempt}/{len(DELAYS)})",
            )
            if client.ping():
                dialog.update(100, message="Backend ready.")
                xbmc.log(
                    f"[Ultimate] Backend is ready (attempt {attempt}).",
                    xbmc.LOGINFO,
                )
                return True
            xbmc.log(
                f"[Ultimate] Backend not ready (attempt {attempt}).",
                xbmc.LOGDEBUG,
            )
    finally:
        dialog.close()

    xbmc.log("[Ultimate] Backend did not respond after all retries.", xbmc.LOGWARNING)
    return False


def build_url(**kwargs):
    # type: (**object) -> str
    return BASE_URL + "?" + urlencode(kwargs)


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

def add_dir(label, url, is_folder=True, art=None, info=None,
            properties=None, is_playable=False):
    li = xbmcgui.ListItem(label, offscreen=True)
    if art:
        li.setArt(art)
    if info:
        li.setInfo("video", info)
    if properties:
        for k, v in properties.items():
            li.setProperty(k, str(v))
    if is_playable:
        li.setProperty("IsPlayable", "true")
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=is_folder)


def end_directory(succeeded=True, cache=True, content="videos",
                  update_listing=False):
    xbmcplugin.setContent(HANDLE, content)
    xbmcplugin.endOfDirectory(HANDLE, succeeded, cacheToDisc=cache,
                               updateListing=update_listing)


# ---------------------------------------------------------------------------
# Root menu
# ---------------------------------------------------------------------------

def show_root():
    client = get_client()

    # On first launch (or after a Kodi restart) the backend process may still
    # be initialising.  Poll until it is reachable before doing real work.
    if not client.ping():
        if not _wait_for_backend(client):
            notify_error(
                "Backend did not start in time. "
                "Check the server and try again."
            )
            end_directory(False)
            return

    try:
        providers = client.get_providers()
    except APIError as e:
        notify_error(f"Cannot connect to server: {e}")
        end_directory(False)
        return

    if not providers:
        notify("No providers available. Check server connection.",
               icon=xbmcgui.NOTIFICATION_WARNING)
        end_directory(False)
        return

    # If there is only one provider, skip the provider-selection sub-menu and
    # go straight into that provider's menu. We still call end_directory() so
    # Kodi's root handle is properly closed.
    if len(providers) == 1:
        p = providers[0]
        _populate_provider_menu(p.get("name", ""))
        end_directory(content="files")
        return

    for p in providers:
        name  = p.get("name", "")
        label = provider_label(p)
        logo  = safe_image_url(p.get("logo", ""))
        art   = {"icon": logo, "thumb": logo, "fanart": logo} if logo else {}

        add_dir(
            label=label,
            url=build_url(action="provider_menu", provider=name),
            art=art,
            info={"title": label},
        )

    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_LABEL)
    end_directory(content="files")


# ---------------------------------------------------------------------------
# Provider sub-menu
# ---------------------------------------------------------------------------

def _populate_provider_menu(provider):
    """Add the fixed provider sub-menu items to the current directory handle."""
    # Channels folder — with a context menu item to export to M3U
    channels_url = build_url(action="channels", provider=provider)
    channels_li  = xbmcgui.ListItem("📺  Channels", offscreen=True)
    export_url   = build_url(action="export_channels_m3u", provider=provider)
    channels_li.addContextMenuItems([
        (M3U_CONTEXT_LABEL, f"RunPlugin({export_url})")
    ])
    xbmcplugin.addDirectoryItem(HANDLE, channels_url, channels_li, isFolder=True)

    add_dir("📅  Events",      build_url(action="events",      provider=provider))
    add_dir("🔴  Live Now",    build_url(action="live_events", provider=provider))
    add_dir("🎬  VOD",         build_url(action="vod",         provider=provider))
    add_dir("🔍  Search VOD",  build_url(action="vod_search",  provider=provider))
    add_dir("⭐  Favorites",   build_url(action="favorites",   provider=provider))
    add_dir("⏺  Recordings",  build_url(action="recordings",  provider=provider))


def show_provider_menu(provider):
    _populate_provider_menu(provider)
    end_directory(content="files")


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

def show_channels(provider):
    client = get_client()
    try:
        channels = client.get_channels(provider)
    except APIError as e:
        notify_error(f"Failed to load channels: {e}")
        end_directory(False)
        return

    show_in_title = get_setting_bool("show_provider_in_title")

    for ch in channels:
        ch_id       = ch.get("Id", "")
        name        = ch.get("Name", ch_id)
        logo        = safe_image_url(ch.get("LogoUrl", ""))
        is_radio    = ch.get("IsRadio", False)
        catchup_hrs = ch.get("CatchupHours", 0)

        label = f"[{provider}] {name}" if show_in_title else name
        art   = {"icon": logo, "thumb": logo} if logo else {}
        info  = {
            "title":     name,
            "mediatype": "video" if not is_radio else "song",
        }

        add_dir(
            label=label,
            url=build_url(action="play_channel", provider=provider,
                          channel_id=ch_id, channel_name=name),
            is_folder=False,
            is_playable=True,
            art=art,
            info=info,
            properties={"CatchupHours": catchup_hrs},
        )

    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_LABEL)
    end_directory(content="livestreams")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def _build_event_item(ev, provider):
    ev_id      = ev.get("Id", "")
    title      = ev.get("Name", ev_id)
    start      = ev.get("StartTime", "")
    status     = ev.get("Status", "")
    duration_m = ev.get("DurationMinutes")
    logo       = safe_image_url(ev.get("LogoUrl", ""))

    status_icon = {"LIVE": "🔴 ", "SCHEDULED": "🕐 ", "ENDED": "✓ "}.get(status, "")
    start_str   = start[11:16] if len(start) >= 16 else ""
    date_str    = start[:10]   if len(start) >= 10 else ""
    time_prefix = f"[{date_str} {start_str}]  " if date_str else ""
    label = f"{status_icon}{time_prefix}{title}"

    art  = {"thumb": logo, "icon": logo} if logo else {}
    info = {"title": title, "mediatype": "video"}
    if date_str:
        info["aired"] = date_str
    if duration_m:
        info["duration"] = int(duration_m) * 60

    return label, art, info, ev_id, title


def show_events(provider):
    client = get_client()
    try:
        events = client.get_events(provider)
    except APIError as e:
        notify_error(f"Failed to load events: {e}")
        end_directory(False)
        return

    if not events:
        notify("No events available for this provider.")
        end_directory()
        return

    for ev in events:
        label, art, info, ev_id, title = _build_event_item(ev, provider)
        add_dir(
            label=label,
            url=build_url(action="play_event", provider=provider,
                          event_id=ev_id, event_name=title),
            is_folder=False,
            is_playable=True,
            art=art,
            info=info,
        )

    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_LABEL)
    end_directory(content="videos")


def show_live_events(provider):
    """Virtual 'Live Now' folder: only events with Status == LIVE."""
    client = get_client()
    try:
        events = client.get_events(provider)
    except APIError as e:
        notify_error(f"Failed to load events: {e}")
        end_directory(False)
        return

    live = [ev for ev in events if ev.get("Status") == "LIVE"]

    if not live:
        notify("No live events right now.")
        end_directory()
        return

    for ev in live:
        label, art, info, ev_id, title = _build_event_item(ev, provider)
        add_dir(
            label=label,
            url=build_url(action="play_event", provider=provider,
                          event_id=ev_id, event_name=title),
            is_folder=False,
            is_playable=True,
            art=art,
            info=info,
        )

    end_directory(content="videos")


# ---------------------------------------------------------------------------
# VOD
# ---------------------------------------------------------------------------

def show_vod(provider, path=None, cursor=None):
    """
    Browse VOD tree with cursor-based paging support.

    API path rules (from real responses):
      - Root:     GET /api/providers/{p}/vod
      - Sub-path: GET /api/providers/{p}/vod/{path}   (no leading slash)

    Entry types in response:
      - vod_category: folder, navigate deeper using its `id` field which is an
                      absolute path like "/sports/alpine-skiing" — strip the
                      leading "/" to get the API path segment.
      - vod (item):   playable, identified by `Id` (PascalCase UUID).

    When the server returns a next_cursor a "Nächste Seite" navigation item
    is appended at the bottom of the listing so the user can load more.
    """
    client = get_client()
    try:
        if path:
            result = client.get_vod_path(provider, path, cursor=cursor)
        else:
            result = client.get_vod_root(provider, cursor=cursor)
    except APIError as e:
        notify_error(f"Failed to load VOD: {e}")
        end_directory(False)
        return

    entries     = result.get("entries", [])
    next_cursor = result.get("next_cursor")

    if not entries:
        notify("No VOD content available.")
        end_directory()
        return

    xbmc.log(
        f"[Ultimate] show_vod: provider={provider} path={repr(path)} "
        f"cursor={repr(cursor)} entries={len(entries)} "
        f"next_cursor={repr(next_cursor)}",
        xbmc.LOGDEBUG,
    )

    for entry in entries:
        entry_type = entry.get("type", "vod")

        if entry_type == "vod_category":
            cat_id      = entry.get("id", "")
            name        = entry.get("name", cat_id)
            description = entry.get("description", "")
            logo        = safe_image_url(entry.get("logo_url", ""))
            art         = {"thumb": logo, "icon": logo} if logo else {}
            child_count = entry.get("child_count")
            count_str   = f"  ({child_count})" if child_count else ""
            api_path    = cat_id.lstrip("/")

            _export_url = build_url(
                action="export_vod_library",
                provider=provider,
                content_id=cat_id,
                folder_name=name,
            )
            cm = [(CONTEXT_MENU_LABEL, f"RunPlugin({_export_url})")]
            li = xbmcgui.ListItem(f"📁  {name}{count_str}", offscreen=True)
            li.setArt(art)
            li.setInfo("video", {"title": name, "plot": description})
            li.addContextMenuItems(cm)
            xbmcplugin.addDirectoryItem(
                HANDLE,
                build_url(action="vod_path", provider=provider, path=api_path),
                li,
                isFolder=True,
            )

        else:
            item_id     = entry.get("Id", "")
            name        = entry.get("Name", item_id)
            logo        = safe_image_url(entry.get("LogoUrl", ""))
            description = entry.get("description", "")
            art         = {"thumb": logo, "icon": logo} if logo else {}
            content_t   = entry.get("ContentType", "VOD")

            duration_s = 0
            try:
                duration_s = int(entry.get("duration_seconds") or 0)
            except (TypeError, ValueError):
                pass

            year      = entry.get("release_year")
            rating    = entry.get("rating")
            director  = entry.get("director")
            cast_list = entry.get("cast") or []
            season    = entry.get("season_number")
            episode   = entry.get("episode_number")

            if season is not None and episode is not None:
                label = f"S{season:02d}E{episode:02d} – {name}"
            else:
                label = name

            info = {
                "title":    name,
                "plot":     description,
                "duration": duration_s,
                "mediatype": ("movie" if content_t == "MOVIE"
                              else "episode" if season else "video"),
            }
            if year:
                try:
                    info["year"] = int(year)
                except (TypeError, ValueError):
                    pass
            if director:
                info["director"] = director
            if cast_list:
                info["cast"] = cast_list
            if rating:
                info["mpaa"] = rating
            if season is not None:
                info["season"] = int(season)
            if episode is not None:
                info["episode"] = int(episode)

            xbmc.log(f"[Ultimate] VOD item: id={item_id} cat_path={repr(path)}",
                     xbmc.LOGDEBUG)

            add_dir(
                label=label,
                url=build_url(action="play_vod", provider=provider,
                              cat_path=path or "", item_id=item_id, item_name=name),
                is_folder=False,
                is_playable=True,
                art=art,
                info=info,
            )

    # ── Pagination: append "Nächste Seite" when more pages exist ──────
    if next_cursor:
        next_url_kwargs = dict(
            action="vod_path" if path else "vod",
            provider=provider,
            cursor=next_cursor,
        )
        if path:
            next_url_kwargs["path"] = path

        xbmc.log(
            f"[Ultimate] show_vod: adding next-page item (cursor={repr(next_cursor)})",
            xbmc.LOGDEBUG,
        )
        add_dir(
            label="⏭  Nächste Seite",
            url=build_url(**next_url_kwargs),
            is_folder=True,
            info={"title": "Nächste Seite"},
        )

    end_directory(content="videos")


def show_vod_search(provider):
    """Prompt the user for a search query and display matching VOD items."""
    query = xbmcgui.Dialog().input("Search VOD", type=xbmcgui.INPUT_ALPHANUM)
    if not query:
        end_directory(False)
        return

    client = get_client()

    try:
        entries = client.search_vod(provider, query)
    except APIError as e:
        notify_error(f"VOD search failed: {e}")
        end_directory(False)
        return

    if not entries:
        notify(f"No results for '{query}'.")
        end_directory()
        return

    for entry in entries:
        entry_type = entry.get("type", "vod")

        if entry_type == "vod_category":
            # Handle category folders
            cat_id      = entry.get("id", "")
            name        = entry.get("name", cat_id)
            description = entry.get("description", "")
            logo        = safe_image_url(entry.get("logo_url", ""))
            art         = {"thumb": logo, "icon": logo} if logo else {}
            child_count = entry.get("child_count")
            count_str   = f"  ({child_count})" if child_count else ""

            # Build API path — categories use the id directly
            api_path = cat_id  # e.g., "program_11229"

            add_dir(
                label=f"📁  {name}{count_str}",
                url=build_url(action="vod_path", provider=provider, path=api_path),
                is_folder=True,
                art=art,
                info={"title": name, "plot": description},
            )
        else:
            # Handle playable VOD items
            item_id     = entry.get("Id", "")
            name        = entry.get("Name", item_id)
            logo        = safe_image_url(entry.get("LogoUrl", ""))
            description = entry.get("description", "")
            art         = {"thumb": logo, "icon": logo} if logo else {}

            cat_path = (
                entry.get("id", "").lstrip("/").rsplit("/", 1)[0]
                if entry.get("id") else ""
            )

            add_dir(
                label=name,
                url=build_url(action="play_vod", provider=provider,
                              cat_path=cat_path, item_id=item_id, item_name=name),
                is_folder=False,
                is_playable=True,
                art=art,
                info={"title": name, "plot": description, "mediatype": "video"},
            )

    end_directory(content="videos")


# ---------------------------------------------------------------------------
# Favorites
# ---------------------------------------------------------------------------

def show_favorites(provider):
    client = get_client()
    try:
        result    = client.get_favorites(provider)
        favorites = result.get("favorites", [])
    except APIError as e:
        notify_error(f"Failed to load favorites: {e}")
        end_directory(False)
        return

    if not favorites:
        notify("No favorites found for this provider.")
        end_directory()
        return

    for fav in favorites:
        fav_type     = fav.get("FavoriteType", "")
        content_id   = fav.get("ContentId", "")
        title        = fav.get("Title", "")
        thumbnail    = safe_image_url(fav.get("ThumbnailUrl", ""))
        series_title = fav.get("SeriesTitle")

        art   = {"thumb": thumbnail, "icon": thumbnail} if thumbnail else {}
        label = f"{title} ({series_title})" if series_title else title
        info  = {"title": title, "mediatype": "video"}
        if series_title:
            info["tvshowtitle"] = series_title

        if fav_type == "CHANNEL":
            # Live channel — play directly
            play_url  = build_url(
                action="play_channel",
                provider=provider,
                channel_id=content_id,
                channel_name=title,
            )
            is_folder    = False
            is_playable  = True

        elif fav_type == "PROGRAM":
            # Series/show — open as a browsable folder (seasons/episodes inside)
            play_url  = build_url(
                action="vod_path",
                provider=provider,
                path=f"program_{content_id}",
            )
            label       = f"📁  {label}"
            is_folder   = True
            is_playable = False

        else:
            # CLIP / MOVIE / EVENT — treat as a directly playable VOD leaf
            play_url  = build_url(
                action="play_vod",
                provider=provider,
                cat_path="",
                item_id=content_id,
                item_name=title,
            )
            is_folder   = False
            is_playable = True

        li = xbmcgui.ListItem(label, offscreen=True)
        if art:
            li.setArt(art)
        li.setInfo("video", info)
        if is_playable:
            li.setProperty("IsPlayable", "true")

        xbmcplugin.addDirectoryItem(HANDLE, play_url, li, isFolder=is_folder)

    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_LABEL)
    end_directory(content="videos")


# ---------------------------------------------------------------------------
# Playback helpers
# ---------------------------------------------------------------------------

# Fields that exist in the Kodi 22 JSON DRM format but have no equivalent
# in the Kodi 21 drm_legacy pipe string.
_LEGACY_UNSUPPORTED_FIELDS = (
    "wrapper", "unwrapper", "req_data", "server_certificate", "req_params"
)


def _has_drm(drm_configs):
    # type: (Optional[dict]) -> bool
    """Return True if any real DRM system (not 'none'/'generic') is present."""
    return any(
        k not in ("none", "generic", "NONE", "GENERIC")
        for k in (drm_configs or {})
    )


def _get_kodi_major_version():
    # type: () -> int
    """Return the Kodi major version as an integer (e.g. 21, 22)."""
    version_string = xbmc.getInfoLabel("System.BuildVersion")
    try:
        return int(version_string.split(".")[0])
    except (ValueError, IndexError):
        xbmc.log(
            f"[Ultimate] Could not parse Kodi version '{version_string}', assuming 22+",
            xbmc.LOGWARNING,
        )
        return 22


def _select_legacy_drm_entry(drm_configs):
    # type: (dict) -> Optional[Tuple[str, dict]]
    """
    Pick the single DRM entry to use for the Kodi 21 legacy property.

    Selection order:
      1. Widevine (com.widevine.alpha) if present — always preferred.
      2. Otherwise, the entry with the lowest priority value.

    Returns (key_system, cfg) tuple, or None if drm_configs is empty.
    """
    if not drm_configs:
        return None

    widevine_key = "com.widevine.alpha"
    if widevine_key in drm_configs:
        return widevine_key, drm_configs[widevine_key]

    return min(drm_configs.items(), key=lambda item: item[1].get("priority", 999))


def _build_drm_legacy_string(drm_configs):
    # type: (dict) -> Optional[str]
    """
    Convert a drm_configs dict (Kodi 22 JSON format) into a single
    pipe-separated drm_legacy string for Kodi 21.

    Returns "KeySystem|LicenseURLorKIDs|Headers", or None if no usable entry.
    """
    entry = _select_legacy_drm_entry(drm_configs)
    if entry is None:
        return None

    key_system, cfg = entry
    license_cfg = cfg.get("license") or {}  # type: dict

    unsupported = [f for f in _LEGACY_UNSUPPORTED_FIELDS if license_cfg.get(f)]
    if unsupported:
        xbmc.log(
            f"[Ultimate] Kodi 21 drm_legacy: field(s) {unsupported} are not supported "
            f"in the legacy format for '{key_system}' and will be ignored. "
            f"Upgrade to Kodi 22 for full DRM feature support.",
            xbmc.LOGWARNING,
        )

    server_url  = license_cfg.get("server_url") or ""   # type: str
    req_headers = license_cfg.get("req_headers") or ""  # type: str
    keyids      = license_cfg.get("keyids") or {}        # type: dict

    if key_system == "org.w3.clearkey":
        if keyids:
            license_or_kids = ",".join(f"{kid}:{key}" for kid, key in keyids.items())
        else:
            license_or_kids = server_url
    else:
        license_or_kids = server_url

    return f"{key_system}|{license_or_kids}|{req_headers}"


def _is_complex_drm_case(drm_configs):
    # type: (dict) -> bool
    """Check if DRM config needs the old license_key format (has unwrapper/wrapper/req_data)."""
    if not drm_configs:
        return False

    cfg = drm_configs.get("com.widevine.alpha")
    if not cfg:
        cfg = next(iter(drm_configs.values())) if drm_configs else None

    if not cfg:
        return False

    license_cfg = cfg.get("license") or {}
    return any(license_cfg.get(field) for field in ("unwrapper", "wrapper", "req_data"))


def _decode_license_req_data(req_data):
    # type: (str) -> str
    if not req_data:
        return ""
    try:
        return b64decode(req_data).decode("utf-8")
    except Exception:
        return ""


def _legacy_post_data_from_req_data(req_data):
    # type: (str) -> str
    decoded = _decode_license_req_data(req_data)
    if decoded == "{CHA-RAW}":
        return "R{SSM}"
    if decoded == "{CHA-B64}":
        return "b{SSM}"
    if decoded == "{CHA-B64U}":
        return "B{SSM}"
    return "R{SSM}"


def _legacy_response_data_from_license(license_cfg):
    # type: (dict) -> str
    unwrapper = (license_cfg.get("unwrapper") or "").lower()
    path_data = (license_cfg.get("unwrapper_params") or {}).get("path_data")

    if unwrapper == "json,base64" and path_data:
        return f"JB{path_data}"
    if unwrapper == "base64":
        return "B"
    return "R"


def _build_old_license_key(drm_configs):
    # type: (dict) -> Optional[Tuple[str, str]]
    """Build old license_key property for Kodi 18-20 and complex Kodi 21 cases."""
    if not drm_configs:
        return None

    cfg = drm_configs.get("com.widevine.alpha")
    if not cfg:
        cfg = next(iter(drm_configs.values())) if drm_configs else None

    if not cfg:
        return None

    key_system  = ("com.widevine.alpha" if "com.widevine.alpha" in drm_configs
                   else next(iter(drm_configs.keys())))
    license_cfg = cfg.get("license") or {}
    server_url  = license_cfg.get("server_url") or ""

    if not server_url:
        return None

    headers       = license_cfg.get("req_headers") or ""
    post_data     = _legacy_post_data_from_req_data(license_cfg.get("req_data") or "")
    response_data = _legacy_response_data_from_license(license_cfg)

    # Ensure Content-Type header is present
    headers_dict = dict(parse_qsl(headers))
    if 'Content-Type' not in headers_dict and 'content-type' not in headers_dict:
        headers_dict['Content-Type'] = 'application/octet-stream'

    # Rebuild headers
    new_headers = urlencode(headers_dict)

    return key_system, "|".join((server_url, new_headers, post_data, response_data))


def _configure_playback(li, stream_url, drm_configs, use_isa, stream_headers=None):
    # type: (xbmcgui.ListItem, str, dict, bool, Optional[dict]) -> bool
    li.setMimeType("application/dash+xml")
    li.setContentLookup(False)

    if not use_isa:
        li.setPath(stream_url)
        return True

    # FIX: verify InputStreamAdaptive is actually installed before using it.
    if not check_inputstream_adaptive():
        notify_error(
            "InputStream Adaptive is not installed or enabled. "
            "Please install it from the Kodi repository."
        )
        return False

    li.setProperty("inputstream", "inputstream.adaptive")

    if _has_drm(drm_configs):
        has_widevine = "com.widevine.alpha" in drm_configs
        if has_widevine and not ensure_widevine():
            notify_error("Widevine CDM not available. Install Widevine or contact support.")
            return False

        kodi_version = _get_kodi_major_version()

        # Strategy 1: Kodi 22+ → JSON DRM
        if kodi_version >= 22:
            li.setProperty("inputstream.adaptive.drm", json.dumps(drm_configs))
            xbmc.log("[Ultimate] DRM Strategy 1 (Kodi 22+ JSON)", xbmc.LOGINFO)

        # Strategies 2 & 3: Kodi <= 21
        else:
            # Strategy 3: Kodi < 21 OR complex DRM case → old license_key
            if kodi_version < 21 or _is_complex_drm_case(drm_configs):
                old_license = _build_old_license_key(drm_configs)
                if old_license:
                    key_system, license_key = old_license
                    li.setMimeType('application/dash+xml')
                    li.setContentLookup(False)
                    li.setProperty("inputstream.adaptive.license_type", key_system)
                    li.setProperty("inputstream.adaptive.license_key", license_key)
                    xbmc.log(
                        f"[Ultimate] DRM Strategy 3 (Kodi {kodi_version}, old license_key)",
                        xbmc.LOGINFO,
                    )
                else:
                    # Best-effort fallback to drm_legacy when old key cannot be built
                    if kodi_version == 21:
                        legacy_string = _build_drm_legacy_string(drm_configs)
                        if legacy_string:
                            li.setProperty("inputstream.adaptive.drm_legacy", legacy_string)
                            xbmc.log(
                                "[Ultimate] DRM Strategy 3 fallback to drm_legacy",
                                xbmc.LOGINFO,
                            )
                        else:
                            xbmc.log(
                                "[Ultimate] DRM Strategy 3: could not build any license "
                                "property — playback may fail.",
                                xbmc.LOGWARNING,
                            )
                    else:
                        xbmc.log(
                            f"[Ultimate] DRM Strategy 3: Kodi {kodi_version} has no "
                            "usable DRM license property — playback may fail.",
                            xbmc.LOGWARNING,
                        )
            else:
                # Strategy 2: Kodi 21 simple case → drm_legacy
                legacy_string = _build_drm_legacy_string(drm_configs)
                if legacy_string:
                    li.setProperty("inputstream.adaptive.drm_legacy", legacy_string)
                    xbmc.log(
                        "[Ultimate] DRM Strategy 2 (Kodi 21 simple drm_legacy)",
                        xbmc.LOGINFO,
                    )
                else:
                    xbmc.log(
                        "[Ultimate] DRM Strategy 2: could not build drm_legacy string "
                        "— playback may fail.",
                        xbmc.LOGWARNING,
                    )

    if stream_headers:
        manifest_hdrs = stream_headers.get("manifest") or {}
        segment_hdrs  = stream_headers.get("segment") or {}

        if manifest_hdrs:
            encoded = "&".join(
                f"{k}={quote(str(v), safe='')}" for k, v in manifest_hdrs.items()
            )
            li.setProperty("inputstream.adaptive.manifest_headers", encoded)

        if segment_hdrs:
            encoded = "&".join(
                f"{k}={quote(str(v), safe='')}" for k, v in segment_hdrs.items()
            )
            li.setProperty("inputstream.adaptive.stream_headers", encoded)

    li.setPath(stream_url)
    return True


def _resolve_drm(drm_from_header, drm_endpoint_fn):
    # type: (Optional[dict], object) -> dict
    """
    Return the best available drm_configs dict.

    Uses the header value when present; calls drm_endpoint_fn() as fallback.
    drm_endpoint_fn should be a zero-argument callable that returns the raw
    /drm API response dict (containing a "drm_configs" key).
    """
    if drm_from_header is not None:
        xbmc.log("[Ultimate] Using DRM config from manifest header", xbmc.LOGDEBUG)
        return drm_from_header

    xbmc.log(
        "[Ultimate] Fetching DRM config from /drm endpoint (header fallback)",
        xbmc.LOGDEBUG,
    )
    try:
        drm_response = drm_endpoint_fn()
        return (drm_response or {}).get("drm_configs", {})
    except APIError as e:
        xbmc.log(f"[Ultimate] /drm endpoint fallback failed: {e}", xbmc.LOGWARNING)
        return {}



def _pick_manifest_url(manifest_data):
    # type: (dict) -> Optional[str]
    """Return the appropriate manifest URL based on the use_software_drm setting.

    When software DRM is enabled and the server returned a sw_drm_manifest_url,
    that URL is used instead of the standard manifest_url.  If the setting is
    enabled but the server did not supply a software-DRM URL (e.g. the content
    or provider does not support it), the function falls back to manifest_url
    and logs a warning so the operator knows the setting had no effect.
    """
    if get_setting_bool("use_software_drm"):
        sw_url = manifest_data.get("sw_drm_manifest_url")
        if sw_url:
            xbmc.log("[Ultimate] Software DRM enabled — using sw_drm_manifest_url", xbmc.LOGINFO)
            return sw_url
        xbmc.log(
            "[Ultimate] Software DRM requested but sw_drm_manifest_url not present "
            "in server response — falling back to standard manifest_url",
            xbmc.LOGWARNING,
        )
    return manifest_data.get("manifest_url")


# ---------------------------------------------------------------------------
# Play Channel
# ---------------------------------------------------------------------------

def play_channel(provider, channel_id, channel_name=""):
    client  = get_client()
    use_isa = get_setting_bool("use_inputstream_adaptive")
    title   = channel_name or channel_id

    # Catchup / time-shift: Kodi appends start_time and end_time to the
    # plugin URL when the user requests a time-shifted stream via the
    # catchup-source attribute in the M3U.  Both will be None for normal
    # live playback.
    start_time = PARAMS.get("start_time") or None
    end_time   = PARAMS.get("end_time")   or None

    try:
        # Always fetch the live manifest — catchup params are NOT sent here.
        # The server returns a catchup_stream_url_template in the body when
        # the channel supports time-shift; we expand it client-side below.
        manifest_data, drm_from_header, stream_headers, catchup_template = \
            client.get_channel_manifest(provider, channel_id)

        if start_time and end_time and catchup_template:
            # Expand the template with our timestamps.  Guard against a
            # malformed template (unexpected placeholders, etc.) so we never
            # let a bare KeyError/IndexError escape to Kodi.
            try:
                stream_url = catchup_template.format(
                    start_time=start_time,
                    end_time=end_time,
                )
                xbmc.log(
                    f"[Ultimate] Catchup template expanded: {stream_url}",
                    xbmc.LOGINFO,
                )
            except (KeyError, IndexError, ValueError) as fmt_err:
                xbmc.log(
                    f"[Ultimate] catchup_stream_url_template expand failed "
                    f"({fmt_err!r}); template={catchup_template!r} — "
                    f"falling back to live stream",
                    xbmc.LOGWARNING,
                )
                stream_url = _pick_manifest_url(manifest_data)
        elif start_time and end_time:
            # Catchup requested but no template from server — log and fall
            # through to live so playback at least starts.
            xbmc.log(
                "[Ultimate] Catchup requested but server returned no "
                "catchup_stream_url_template — playing live stream instead",
                xbmc.LOGWARNING,
            )
            stream_url = _pick_manifest_url(manifest_data)
        else:
            stream_url = _pick_manifest_url(manifest_data)

        if not stream_url:
            notify_error("No manifest URL returned by server.")
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem(title))
            return
    except APIError as e:
        notify_error(f"Failed to get channel manifest: {e}")
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem(title))
        return

    # Fetch DRM configs via the unified helper.  For catchup streams the DRM
    # endpoint also accepts start_time/end_time; the lambda forwards them so
    # the /drm fallback path works correctly for time-shifted content.
    # drm_from_header (captured from the live manifest response) is still
    # valid for catchup — the licence server URL does not change per segment.
    drm_configs = {}
    if use_isa:
        drm_configs = _resolve_drm(
            drm_from_header,
            lambda: client.get_channel_drm(
                provider, channel_id, start_time=start_time, end_time=end_time
            ),
        )

    li = xbmcgui.ListItem(title, offscreen=True)
    li.setInfo("video", {"title": title})
    ok = _configure_playback(li, stream_url, drm_configs, use_isa, stream_headers)
    xbmcplugin.setResolvedUrl(HANDLE, ok, li)


# ---------------------------------------------------------------------------
# Play Event
# ---------------------------------------------------------------------------

def play_event(provider, event_id, event_name=""):
    client  = get_client()
    use_isa = get_setting_bool("use_inputstream_adaptive")
    title   = event_name or event_id

    try:
        manifest_data, drm_from_header, stream_headers = client.get_event_manifest(
            provider, event_id
        )
        stream_url = _pick_manifest_url(manifest_data)
        if not stream_url:
            notify_error("No manifest URL returned by server.")
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem(title))
            return
    except APIError as e:
        notify_error(f"Failed to get event manifest: {e}")
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem(title))
        return

    drm_configs = {}
    if use_isa:
        drm_configs = _resolve_drm(
            drm_from_header,
            lambda: client.get_event_drm(provider, event_id),
        )

    li = xbmcgui.ListItem(title, offscreen=True)
    li.setInfo("video", {"title": title})
    ok = _configure_playback(li, stream_url, drm_configs, use_isa, stream_headers)
    xbmcplugin.setResolvedUrl(HANDLE, ok, li)


# ---------------------------------------------------------------------------
# Play VOD
# ---------------------------------------------------------------------------

def play_vod(provider, cat_path="", item_id="", item_name=""):
    """Play VOD item using the content_id directly (no path construction)."""
    title   = item_name or item_id
    client  = get_client()
    use_isa = get_setting_bool("use_inputstream_adaptive")

    xbmc.log(f"[Ultimate] play_vod: using direct item_id: {item_id}", xbmc.LOGINFO)

    try:
        manifest_data, drm_from_header, stream_headers = client.get_vod_manifest_by_id(
            provider, item_id
        )
        stream_url = _pick_manifest_url(manifest_data)
        if not stream_url:
            notify_error("No manifest URL returned by server.")
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem(title, offscreen=True))
            return
    except APIError as e:
        notify_error(f"Failed to get VOD manifest: {e}")
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem(title, offscreen=True))
        return

    drm_configs = {}
    if use_isa:
        drm_configs = _resolve_drm(
            drm_from_header,
            lambda: client.get_vod_drm(provider, item_id),
        )

    li = xbmcgui.ListItem(title, offscreen=True)
    li.setInfo("video", {"title": title})
    ok = _configure_playback(li, stream_url, drm_configs, use_isa, stream_headers)
    xbmcplugin.setResolvedUrl(HANDLE, ok, li)


# ---------------------------------------------------------------------------
# Recordings
# ---------------------------------------------------------------------------

def show_recordings(provider):
    client = get_client()
    try:
        recordings = client.get_recordings(provider)
    except APIError as e:
        notify_error(f"Failed to load recordings: {e}")
        end_directory(False)
        return

    # Only show recordings that have started capturing — PENDING is not yet
    # playable, FAILED/DELETED have no usable content.
    PLAYABLE_STATUSES = {"COMPLETED", "RECORDING"}
    recordings = [r for r in recordings if r.get("Status") in PLAYABLE_STATUSES]

    if not recordings:
        notify("No recordings available for this provider.")
        end_directory()
        return

    for rec in recordings:
        rec_id = rec.get("Id", "")
        name   = rec.get("Name") or rec_id

        thumb  = safe_image_url(
            rec.get("ThumbnailUrl") or rec.get("LogoUrl") or rec.get("IconPath", "")
        )
        fanart = safe_image_url(rec.get("FanartUrl", ""))
        art    = {"thumb": thumb, "icon": thumb}
        if fanart:
            art["fanart"] = fanart

        start    = rec.get("RecordingTime", "") or ""
        date_str = start[:10]   if len(start) >= 10 else ""
        time_str = start[11:16] if len(start) >= 16 else ""

        season  = rec.get("SeasonNumber")
        episode = rec.get("EpisodeNumber")
        ep_name = rec.get("EpisodeName")

        ep_prefix   = f"S{int(season):02d}E{int(episode):02d} – " if (
            season is not None and episode is not None
        ) else ""
        time_prefix = f"[{date_str} {time_str}]  " if date_str else ""
        label       = f"{time_prefix}{ep_prefix}{name}"

        description = (
            rec.get("Plot")
            or rec.get("PlotOutline")
            or rec.get("Description", "")
        )

        content_type = rec.get("ContentType", "")
        if season is not None or episode is not None:
            mediatype = "episode"
        elif content_type == "MOVIE":
            mediatype = "movie"
        else:
            mediatype = "video"

        info = {
            "title":     ep_name or name,
            "plot":      description,
            "mediatype": mediatype,
        }

        channel = rec.get("ChannelName", "")
        if channel:
            info["studio"] = channel
        if date_str:
            info["aired"] = date_str

        duration = rec.get("DurationSeconds")
        if duration:
            try:
                info["duration"] = int(duration)
            except (TypeError, ValueError):
                pass

        year = rec.get("ReleaseYear")
        if year:
            try:
                info["year"] = int(year)
            except (TypeError, ValueError):
                pass

        if season is not None:
            info["season"] = int(season)
        if episode is not None:
            info["episode"] = int(episode)

        series_title = rec.get("SeriesTitle")
        if series_title:
            info["tvshowtitle"] = series_title

        genre = rec.get("GenreDescription") or rec.get("Genre", "")
        if genre:
            info["genre"] = genre

        play_count = rec.get("PlayCount", 0)
        if play_count:
            try:
                info["playcount"] = int(play_count)
            except (TypeError, ValueError):
                pass

        resume_pos = rec.get("LastPlayedPosition", 0)
        properties = {}
        if resume_pos:
            try:
                properties["ResumeTime"] = str(int(resume_pos))
                properties["TotalTime"]  = str(int(duration)) if duration else "0"
            except (TypeError, ValueError):
                pass

        if rec.get("Status") == "RECORDING":
            label = f"🔴 {label}"

        add_dir(
            label=label,
            url=build_url(action="play_recording", provider=provider,
                          recording_id=rec_id, recording_name=name),
            is_folder=False,
            is_playable=True,
            art=art,
            info=info,
            properties=properties or None,
        )

    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_LABEL)
    end_directory(content="videos")


def play_recording(provider, recording_id, recording_name=""):
    client  = get_client()
    use_isa = get_setting_bool("use_inputstream_adaptive")
    title   = recording_name or recording_id

    try:
        manifest_data, drm_from_header, stream_headers = client.get_recording_manifest(
            provider, recording_id
        )
        stream_url = _pick_manifest_url(manifest_data)
        if not stream_url:
            notify_error("No manifest URL returned by server.")
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem(title))
            return
    except APIError as e:
        notify_error(f"Failed to get recording manifest: {e}")
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem(title))
        return

    drm_configs = {}
    if use_isa:
        drm_configs = _resolve_drm(
            drm_from_header,
            lambda: client.get_recording_drm(provider, recording_id),
        )

    li = xbmcgui.ListItem(title, offscreen=True)
    li.setInfo("video", {"title": title})
    ok = _configure_playback(li, stream_url, drm_configs, use_isa, stream_headers)
    xbmcplugin.setResolvedUrl(HANDLE, ok, li)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def router():
    action = PARAMS.get("action", "root")

    if action == "root":
        show_root()
    elif action == "provider_menu":
        show_provider_menu(PARAMS.get("provider", ""))
    elif action == "channels":
        show_channels(PARAMS.get("provider", ""))
    elif action == "events":
        show_events(PARAMS.get("provider", ""))
    elif action == "live_events":
        show_live_events(PARAMS.get("provider", ""))
    elif action == "vod":
        show_vod(PARAMS.get("provider", ""), cursor=PARAMS.get("cursor") or None)
    elif action == "vod_path":
        show_vod(
            PARAMS.get("provider", ""),
            PARAMS.get("path", ""),
            cursor=PARAMS.get("cursor") or None,
        )
    elif action == "vod_search":
        show_vod_search(PARAMS.get("provider", ""))
    elif action == "favorites":
        show_favorites(PARAMS.get("provider", ""))
    elif action == "play_channel":
        play_channel(
            provider=PARAMS.get("provider", ""),
            channel_id=PARAMS.get("channel_id", ""),
            channel_name=PARAMS.get("channel_name", ""),
        )
    elif action == "play_event":
        play_event(
            provider=PARAMS.get("provider", ""),
            event_id=PARAMS.get("event_id", ""),
            event_name=PARAMS.get("event_name", ""),
        )
    elif action == "play_vod":
        play_vod(
            provider=PARAMS.get("provider", ""),
            cat_path=PARAMS.get("cat_path", ""),
            item_id=PARAMS.get("item_id", ""),
            item_name=PARAMS.get("item_name", ""),
        )
    elif action == "recordings":
        show_recordings(PARAMS.get("provider", ""))
    elif action == "play_recording":
        play_recording(
            provider=PARAMS.get("provider", ""),
            recording_id=PARAMS.get("recording_id", ""),
            recording_name=PARAMS.get("recording_name", ""),
        )
    elif action == "export_vod_library":
        export_vod_library(
            PARAMS.get("provider", ""),
            PARAMS.get("content_id", ""),
            PARAMS.get("folder_name", ""),
        )
    elif action == "export_channels_m3u":
        export_channels_m3u(PARAMS.get("provider", ""))
    else:
        xbmc.log(f"[Ultimate] Unknown action: {action}", xbmc.LOGWARNING)
        show_root()


if __name__ == "__main__":
    router()