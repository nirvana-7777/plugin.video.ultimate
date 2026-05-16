#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vod_export.py – VOD-to-Kodi-library export for Ultimate Backend addon.

Exported public surface
-----------------------
    export_vod_library(provider, content_id, folder_name)  ← called from router()
    CONTEXT_MENU_LABEL                                      ← label for context menu

Integration into addon.py
--------------------------
1.  Imports (add after the existing `from utils import …` block):

        from vod_export import export_vod_library, CONTEXT_MENU_LABEL

2.  show_vod() – inside the `vod_category` branch, replace the add_dir() call
    with the version below that attaches a context menu item.
    `cat_id` and `name` are already defined at that point in the loop.

        cm = [(
            CONTEXT_MENU_LABEL,
            f'RunPlugin({build_url(action="export_vod_library",
                                   provider=provider,
                                   content_id=cat_id,
                                   folder_name=name)})'
        )]
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

    Note: add_dir() does not expose context-menu support, so the ListItem is
    constructed inline — this is the only structural change needed in show_vod().

3.  router() – add before the final `else` clause:

        elif action == "export_vod_library":
            export_vod_library(
                PARAMS.get("provider", ""),
                PARAMS.get("content_id", ""),
                PARAMS.get("folder_name", ""),
            )

Design notes
------------
- content_id is the opaque API identifier for the category being exported
  (e.g. "folder_4", "program_69946").  It is what the API expects as the
  path segment in GET /api/providers/{p}/vod/{content_id}.
  It is NOT a slash-separated filesystem path.

- folder_name is the human-readable category name used to name the export
  directory.  Passing it from the context menu avoids an extra API round-trip.

- Export root is read from addon setting "export_root_path".
  Fallback: special://profile/addon_data/plugin.video.ultimate/library
  (always writable on every Kodi platform).

- File layout:
    <root>/<provider>/vod/<folder_name>/
        Movie Name (2024).strm
        Movie Name (2024).nfo
        poster.jpg                (only when LogoUrl present and file absent)

    <root>/<provider>/vod/<folder_name>/
        tvshow.nfo
        poster.jpg
        Season 01/
            Show Name S01E01.strm
            Show Name S01E01.nfo

- Recursive sub-category directories are named from the category's `name`
  field returned by the API, not from the opaque `id`.

- Series detection: a leaf folder whose items all carry season_number is
  treated as a series.  tvshow.nfo is written at the show folder level.

- Deletion sync: expected file set is built first; stale .strm/.nfo/poster.jpg
  files and then empty directories are removed ONLY after a fully-successful
  crawl.  Any API error during crawl aborts the delete pass entirely.

- Poster strategy: write once; skip if file already present.  Re-run export
  to refresh deliberately removed posters.

- Long-running safety: uses xbmc.Monitor().abortRequested() throughout; a
  DialogProgressBG keeps the user informed. Cancellation is via Kodi's
  own abort signal (xbmc.Monitor) since DialogProgressBG has no cancel button.
"""

import os
import re
import threading
import xml.etree.ElementTree as ET
from xml.dom import minidom
from typing import Dict, List, Optional, Set, Tuple

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

try:
    from urllib.request import urlretrieve
    from urllib.error import URLError
except ImportError:                          # Kodi Python 2 remnant – not expected
    from urllib import urlretrieve           # type: ignore
    from urllib2 import URLError             # type: ignore

from api import UltimateBackendClient, APIError
from utils import get_setting, get_setting_bool, notify, notify_error, safe_image_url

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

CONTEXT_MENU_LABEL = "Export to Library"

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_ADDON     = xbmcaddon.Addon()
_LOG_TAG   = "[Ultimate/Export]"
_STALE_EXT = {".strm", ".nfo"}          # extensions cleaned up during sync
_ILLEGAL   = re.compile(r'[\\/:*?"<>|]')  # filesystem-unsafe chars (cross-platform)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _log(msg, level=xbmc.LOGDEBUG):
    xbmc.log(f"{_LOG_TAG} {msg}", level)


def _sanitise(name):
    # type: (str) -> str
    """Replace filesystem-unsafe characters with an underscore."""
    return _ILLEGAL.sub("_", name).strip()


def _resolve_export_root():
    # type: () -> str
    """
    Return the absolute export root path from the addon setting, or a safe
    default inside Kodi's own userdata directory.
    """
    from_setting = get_setting("export_root_path") or ""
    if from_setting:
        return xbmcvfs.translatePath(from_setting.rstrip("/\\"))
    default = "special://profile/addon_data/plugin.video.ultimate/library"
    return xbmcvfs.translatePath(default)


def _build_plugin_url(provider, item_id):
    # type: (str, str) -> str
    """Construct the plugin:// playback URL written into .strm files."""
    from urllib.parse import urlencode
    params = urlencode({
        "action":    "play_vod",
        "provider":  provider,
        "item_id":   item_id,
    })
    return f"plugin://plugin.video.ultimate/?{params}"


def _get_client():
    # type: () -> UltimateBackendClient
    ip       = get_setting("server_ip") or "localhost"
    raw_port = get_setting("server_port") or "8000"
    try:
        port = int(raw_port)
    except (ValueError, TypeError):
        port = 8000
    use_https = get_setting_bool("use_https")
    return UltimateBackendClient(ip, port, use_https=use_https)


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _ensure_dir(path):
    # type: (str) -> None
    os.makedirs(path, exist_ok=True)


def _write_text(path, content):
    # type: (str, str) -> None
    """Write text atomically via a sibling temp file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


def _download_poster(url, dest_path):
    # type: (str, str) -> None
    """Download poster only if the file does not already exist."""
    if os.path.exists(dest_path):
        return
    try:
        urlretrieve(url, dest_path + ".tmp")
        os.replace(dest_path + ".tmp", dest_path)
        _log(f"Poster saved: {dest_path}", xbmc.LOGINFO)
    except (URLError, OSError, IOError) as exc:
        _log(f"Poster download failed ({url}): {exc}", xbmc.LOGWARNING)
        # Remove partial download if present
        tmp = dest_path + ".tmp"
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _remove_stale(directory, expected_files):
    # type: (str, Set[str]) -> None
    """
    Walk *directory* and remove .strm / .nfo / poster.jpg files that are
    not in the expected_files set.  Afterwards remove now-empty directories
    bottom-up.
    """
    for dirpath, dirnames, filenames in os.walk(directory, topdown=False):
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            _, ext = os.path.splitext(fname)
            if ext in _STALE_EXT or fname == "poster.jpg":
                if fpath not in expected_files:
                    try:
                        os.remove(fpath)
                        _log(f"Removed stale file: {fpath}", xbmc.LOGINFO)
                    except OSError as exc:
                        _log(f"Could not remove {fpath}: {exc}", xbmc.LOGWARNING)
        # Remove the directory if it is now empty (and is not the root itself)
        if dirpath != directory:
            try:
                os.rmdir(dirpath)
                _log(f"Removed empty dir: {dirpath}", xbmc.LOGDEBUG)
            except OSError:
                pass   # not empty – that is fine


# ---------------------------------------------------------------------------
# NFO builders
# ---------------------------------------------------------------------------

def _pretty_xml(root_element):
    # type: (ET.Element) -> str
    raw = ET.tostring(root_element, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ", encoding=None)


def _nfo_movie(entry):
    # type: (dict) -> str
    movie = ET.Element("movie")
    _el(movie, "title",       entry.get("Name", ""))
    _el(movie, "plot",        entry.get("description", ""))
    _el(movie, "mpaa",        entry.get("rating", ""))
    _el(movie, "director",    entry.get("director", ""))
    year = entry.get("release_year")
    if year:
        _el(movie, "year", str(year))
    for actor in (entry.get("cast") or []):
        actor_el = ET.SubElement(movie, "actor")
        if isinstance(actor, dict):
            _el(actor_el, "name", actor.get("name", str(actor)))
        else:
            _el(actor_el, "name", str(actor))
    return _pretty_xml(movie)


def _nfo_episode(entry):
    # type: (dict) -> str
    ep = ET.Element("episodedetails")
    _el(ep, "title",         entry.get("Name", ""))
    _el(ep, "plot",          entry.get("description", ""))
    season  = entry.get("season_number")
    episode = entry.get("episode_number")
    if season  is not None: _el(ep, "season",  str(int(season)))
    if episode is not None: _el(ep, "episode", str(int(episode)))
    _el(ep, "mpaa",     entry.get("rating",   ""))
    _el(ep, "director", entry.get("director", ""))
    return _pretty_xml(ep)


def _nfo_tvshow(name, description="", logo_url=""):
    # type: (str, str, str) -> str
    tv = ET.Element("tvshow")
    _el(tv, "title", name)
    _el(tv, "plot",  description)
    if logo_url:
        _el(tv, "thumb", logo_url)
    return _pretty_xml(tv)


def _el(parent, tag, text):
    # type: (ET.Element, str, str) -> ET.Element
    """Add a child element only when text is non-empty."""
    el = ET.SubElement(parent, tag)
    if text:
        el.text = text
    return el


# ---------------------------------------------------------------------------
# Core recursive crawler
# ---------------------------------------------------------------------------

class _ExportJob:
    """
    Encapsulates a single export run.  Instantiate and call .run().

    Parameters
    ----------
    client      : active API client
    provider    : provider name string
    content_id  : opaque API category ID (e.g. "folder_4", "program_69946")
    root_dir    : absolute filesystem path for this category's export directory
    progress    : DialogProgressBG instance (already created, not closed)
    monitor     : xbmc.Monitor instance for abort checks
    """

    def __init__(self, client, provider, content_id, root_dir, progress, monitor):
        self._client      = client
        self._provider    = provider
        self._content_id  = content_id
        self._root_dir    = root_dir        # <export_root>/<provider>/vod/<folder_name>
        self._progress    = progress
        self._monitor     = monitor
        self._expected    = set()           # type: Set[str]  – all files we write
        self._crawl_ok    = False
        self._item_count  = 0
        self._folder_count = 0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self):
        # type: () -> Tuple[int, int]
        """
        Execute the export.  Returns (items_written, folders_created).
        Raises nothing – all errors are logged and surfaced via notify_error.
        """
        _log(f"Starting export: provider={self._provider} content_id={self._content_id}",
             xbmc.LOGINFO)
        try:
            self._crawl(self._content_id, self._root_dir, depth=0)
            self._crawl_ok = True
        except _AbortRequested:
            _log("Export aborted by user or Kodi shutdown.", xbmc.LOGINFO)
            return self._item_count, self._folder_count
        except APIError as exc:
            _log(f"API error during crawl: {exc}", xbmc.LOGERROR)
            notify_error(f"Export failed (API error): {exc}")
            return self._item_count, self._folder_count

        if self._crawl_ok:
            _log("Crawl successful – removing stale files.", xbmc.LOGINFO)
            self._progress.update(95, message="Removing stale files…")
            _remove_stale(self._root_dir, self._expected)

        return self._item_count, self._folder_count

    # ------------------------------------------------------------------
    # Recursive crawl
    # ------------------------------------------------------------------

    def _crawl(self, content_id, fs_dir, depth):
        # type: (str, str, int) -> None
        """
        Recursively fetch all pages for content_id and write files under fs_dir.
        content_id is the opaque API identifier passed to get_vod_path().
        fs_dir is the human-named filesystem directory for this category.
        """
        cursor = None
        items_in_folder = []       # type: List[dict]
        category_name   = ""
        category_desc   = ""
        category_logo   = ""

        # ── Paginated fetch ──────────────────────────────────────────────
        while True:
            self._check_abort()
            _log(f"Fetching content_id={repr(content_id)} cursor={repr(cursor)}",
                 xbmc.LOGDEBUG)
            try:
                result = self._client.get_vod_path(
                    self._provider, content_id, cursor=cursor
                )
            except APIError:
                raise

            entries     = result.get("entries", [])
            next_cursor = result.get("next_cursor")

            # Capture folder metadata from the first page only.
            # The API may return a "name" field on the folder response;
            # fall back to content_id itself (never rsplit on a slash —
            # content IDs are opaque tokens, not slash-separated paths).
            if cursor is None:
                category_name = result.get("name", content_id)
                category_desc = result.get("description", "")
                category_logo = safe_image_url(result.get("logo_url", ""))

            items_in_folder.extend(entries)

            if not next_cursor:
                break
            cursor = next_cursor

        if not items_in_folder:
            return

        # ── Separate categories from playable items ──────────────────────
        sub_cats  = [e for e in items_in_folder if e.get("type") == "vod_category"]
        vod_items = [e for e in items_in_folder if e.get("type") != "vod_category"]

        # ── Series detection ─────────────────────────────────────────────
        is_series = bool(vod_items) and all(
            e.get("season_number") is not None for e in vod_items
        )

        # ── Ensure target directory ───────────────────────────────────────
        _ensure_dir(fs_dir)
        self._folder_count += 1

        # ── Write tvshow.nfo and folder poster for series roots ───────────
        if is_series:
            tvshow_nfo = os.path.join(fs_dir, "tvshow.nfo")
            _write_text(tvshow_nfo, _nfo_tvshow(
                category_name, category_desc, category_logo
            ))
            self._expected.add(tvshow_nfo)
            _log(f"tvshow.nfo → {tvshow_nfo}", xbmc.LOGINFO)

            if category_logo:
                poster_path = os.path.join(fs_dir, "poster.jpg")
                _download_poster(category_logo, poster_path)
                self._expected.add(poster_path)

        # ── Write playable VOD items ──────────────────────────────────────
        for entry in vod_items:
            self._check_abort()
            self._write_item(entry, fs_dir, is_series)

        # ── Recurse into sub-categories ───────────────────────────────────
        for cat_entry in sub_cats:
            self._check_abort()
            child_content_id = cat_entry.get("id", "")   # opaque ID, never has a leading /
            child_name       = _sanitise(cat_entry.get("name", child_content_id))
            child_dir        = os.path.join(fs_dir, child_name)

            msg = f"Exporting: {child_name}"
            self._progress.update(-1, message=msg)

            self._crawl(child_content_id, child_dir, depth + 1)

    # ------------------------------------------------------------------
    # Write a single playable item
    # ------------------------------------------------------------------

    def _write_item(self, entry, fs_dir, in_series):
        # type: (dict, str, bool) -> None
        item_id     = entry.get("Id", "")
        name        = entry.get("Name", item_id)
        content_t   = entry.get("ContentType", "VOD")
        season      = entry.get("season_number")
        episode     = entry.get("episode_number")
        year        = entry.get("release_year")
        logo        = safe_image_url(entry.get("LogoUrl", ""))

        # ── Determine file base name ──────────────────────────────────────
        if in_series and season is not None and episode is not None:
            # Season sub-directory
            season_dir  = os.path.join(
                fs_dir, f"Season {int(season):02d}"
            )
            _ensure_dir(season_dir)
            show_title  = _sanitise(
                entry.get("SeriesTitle") or
                os.path.basename(fs_dir)
            )
            base_name   = _sanitise(
                f"{show_title} S{int(season):02d}E{int(episode):02d}"
            )
            target_dir  = season_dir
        elif content_t == "MOVIE":
            year_str    = f" ({int(year)})" if year else ""
            base_name   = _sanitise(f"{name}{year_str}")
            target_dir  = fs_dir
        else:
            base_name   = _sanitise(name)
            target_dir  = fs_dir

        strm_path = os.path.join(target_dir, base_name + ".strm")
        nfo_path  = os.path.join(target_dir, base_name + ".nfo")

        # ── Write .strm ───────────────────────────────────────────────────
        _write_text(strm_path, _build_plugin_url(self._provider, item_id))
        self._expected.add(strm_path)
        self._item_count += 1

        # ── Write .nfo ────────────────────────────────────────────────────
        if in_series and season is not None:
            nfo_content = _nfo_episode(entry)
        elif content_t == "MOVIE":
            nfo_content = _nfo_movie(entry)
        else:
            nfo_content = _nfo_movie(entry)   # generic fallback uses movie schema

        _write_text(nfo_path, nfo_content)
        self._expected.add(nfo_path)

        # ── Poster ────────────────────────────────────────────────────────
        if logo:
            poster_path = os.path.join(target_dir, "poster.jpg")
            _download_poster(logo, poster_path)
            self._expected.add(poster_path)

        # ── Season poster (write once per season dir) ────────────────────
        if in_series and season is not None:
            season_poster = os.path.join(season_dir, "poster.jpg")
            if logo and not os.path.exists(season_poster):
                _download_poster(logo, season_poster)
            self._expected.add(season_poster)

        _log(
            f"Written: {os.path.basename(strm_path)} → {target_dir}",
            xbmc.LOGDEBUG,
        )

        # ── Progress update ───────────────────────────────────────────────
        self._progress.update(-1, message=f"Items: {self._item_count}  –  {name}")

    # ------------------------------------------------------------------
    # Abort check
    # ------------------------------------------------------------------

    def _check_abort(self):
        if self._monitor.abortRequested():
            raise _AbortRequested()


class _AbortRequested(Exception):
    pass


# ---------------------------------------------------------------------------
# Public entry point – called from router()
# ---------------------------------------------------------------------------

def export_vod_library(provider, content_id, folder_name=""):
    # type: (str, str, str) -> None
    """
    Launch the VOD export for *content_id* under *provider* in a background
    thread so Kodi's UI stays responsive.

    Parameters
    ----------
    provider    : provider name (e.g. "rtlplus")
    content_id  : opaque category ID as returned by the API (e.g. "folder_4")
    folder_name : human-readable category name used to name the export directory;
                  passed from the context menu to avoid an extra API round-trip.
                  Falls back to content_id if empty.

    This is the only symbol the router needs to call.
    """
    if not provider or not content_id:
        notify_error("Export: missing provider or content ID.")
        return

    display_name = _sanitise(folder_name or content_id)
    export_root  = _resolve_export_root()

    # <export_root>/<provider>/vod/<human_folder_name>
    # content_id is the opaque API token ("folder_4", "program_69946") —
    # it is NOT used as a filesystem path component.
    target_dir = os.path.join(
        export_root,
        _sanitise(provider),
        "vod",
        display_name,
    )

    _log(
        f"export_vod_library: provider={provider} content_id={content_id} "
        f"folder_name={repr(display_name)} target={target_dir}",
        xbmc.LOGINFO,
    )

    def _run():
        monitor  = xbmc.Monitor()
        progress = xbmcgui.DialogProgressBG()
        progress.create(
            "Ultimate – Export VOD",
            f"Preparing export of '{display_name}'…",
        )
        try:
            client = _get_client()
            job    = _ExportJob(
                client, provider, content_id, target_dir, progress, monitor
            )
            items, folders = job.run()
            progress.update(100, message="Done.")
        except Exception as exc:                    # pragma: no cover – safety net
            _log(f"Unexpected error: {exc}", xbmc.LOGERROR)
            notify_error(f"Export failed unexpectedly: {exc}")
        else:
            if items or folders:
                notify(
                    f"Export complete: {items} items in {folders} folder(s).",
                    icon=xbmcgui.NOTIFICATION_INFO,
                )
            else:
                notify("Export finished — no items found.")
        finally:
            progress.close()

    thread = threading.Thread(target=_run, name="vod_export", daemon=True)
    thread.start()
