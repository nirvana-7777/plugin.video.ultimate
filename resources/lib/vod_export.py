#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vod_export.py – VOD-to-Kodi-library export for Ultimate Backend addon.

Exports VOD categories to Kodi's library format with proper .strm and .nfo files.
"""

import os
import xml.etree.ElementTree as ET
from xml.dom import minidom

import xbmc
import xbmcgui

from utils import (
    EXPORT_TYPE_LIBRARY,
    atomic_write,
    download_image,
    ensure_directory,
    ensure_export_path,
    get_client_from_settings,
    notify,
    notify_error,
    run_in_background,
    safe_image_url,
    sanitise_filename,
    ExportProgress,
)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

CONTEXT_MENU_LABEL = "Export to Library"
_LOG_TAG = "[Ultimate/VODExport]"
_STALE_EXT = {".strm", ".nfo"}  # extensions cleaned up during sync


def _log(msg, level=xbmc.LOGDEBUG):
    xbmc.log(f"{_LOG_TAG} {msg}", level)


def _build_plugin_url(provider, item_id):
    """Construct the plugin:// playback URL written into .strm files."""
    from urllib.parse import urlencode
    params = urlencode({
        "action": "play_vod",
        "provider": provider,
        "item_id": item_id,
    })
    return f"plugin://plugin.video.ultimate/?{params}"


def _pretty_xml(root_element):
    """Convert XML element to pretty-printed string."""
    raw = ET.tostring(root_element, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ", encoding=None)


def _el(parent, tag, text):
    """Add a child element only when text is non-empty."""
    el = ET.SubElement(parent, tag)
    if text:
        el.text = text
    return el


def _nfo_movie(entry):
    """Generate NFO for a movie."""
    movie = ET.Element("movie")
    _el(movie, "title", entry.get("Name", ""))
    _el(movie, "plot", entry.get("description", ""))
    _el(movie, "mpaa", entry.get("rating", ""))
    _el(movie, "director", entry.get("director", ""))

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
    """Generate NFO for a TV episode."""
    ep = ET.Element("episodedetails")
    _el(ep, "title", entry.get("Name", ""))
    _el(ep, "plot", entry.get("description", ""))

    season = entry.get("season_number")
    episode = entry.get("episode_number")
    if season is not None:
        _el(ep, "season", str(int(season)))
    if episode is not None:
        _el(ep, "episode", str(int(episode)))

    _el(ep, "mpaa", entry.get("rating", ""))
    _el(ep, "director", entry.get("director", ""))
    return _pretty_xml(ep)


def _nfo_tvshow(name, description="", logo_url=""):
    """Generate NFO for a TV show series."""
    tv = ET.Element("tvshow")
    _el(tv, "title", name)
    _el(tv, "plot", description)
    if logo_url:
        _el(tv, "thumb", logo_url)
    return _pretty_xml(tv)


def _remove_stale(directory, expected_files):
    """
    Walk directory and remove .strm / .nfo / poster.jpg files that are
    not in the expected_files set. Afterwards remove empty directories.
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

        # Remove directory if empty (and not the root)
        if dirpath != directory:
            try:
                os.rmdir(dirpath)
                _log(f"Removed empty dir: {dirpath}", xbmc.LOGDEBUG)
            except OSError:
                pass  # not empty - that's fine


class _ExportJob:
    """
    Encapsulates a single export run.

    Parameters
    ----------
    client      : active API client
    provider    : provider name string
    content_id  : opaque API category ID (e.g. "folder_4", "program_69946")
    root_dir    : absolute filesystem path for this category's export directory
    progress    : ExportProgress instance
    """

    def __init__(self, client, provider, content_id, root_dir, progress):
        self._client = client
        self._provider = provider
        self._content_id = content_id
        self._root_dir = root_dir
        self._progress = progress
        self._expected = set()  # Set[str] – all files we write
        self._crawl_ok = False
        self._item_count = 0
        self._folder_count = 0

    def run(self):
        """Execute the export. Returns (items_written, folders_created)."""
        _log(f"Starting export: provider={self._provider} content_id={self._content_id}",
             xbmc.LOGINFO)

        try:
            self._crawl(self._content_id, self._root_dir, depth=0)
            self._crawl_ok = True
        except Exception as exc:
            if isinstance(exc, _AbortRequested):
                _log("Export aborted by user or Kodi shutdown.", xbmc.LOGINFO)
            else:
                _log(f"API error during crawl: {exc}", xbmc.LOGERROR)
                notify_error(f"Export failed (API error): {exc}")
            return self._item_count, self._folder_count

        if self._crawl_ok:
            _log("Crawl successful – removing stale files.", xbmc.LOGINFO)
            self._progress.update(95, message="Removing stale files…")
            _remove_stale(self._root_dir, self._expected)

        return self._item_count, self._folder_count

    def _crawl(self, content_id, fs_dir, depth):
        """
        Recursively fetch all pages for content_id and write files under fs_dir.
        """
        cursor = None
        items_in_folder = []
        category_name = ""
        category_desc = ""
        category_logo = ""

        # Paginated fetch
        while True:
            self._check_abort()
            _log(f"Fetching content_id={repr(content_id)} cursor={repr(cursor)}",
                 xbmc.LOGDEBUG)

            result = self._client.get_vod_path(
                self._provider, content_id, cursor=cursor
            )

            entries = result.get("entries", [])
            next_cursor = result.get("next_cursor")

            # Capture folder metadata from first page
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

        # Separate categories from playable items
        sub_cats = [e for e in items_in_folder if e.get("type") == "vod_category"]
        vod_items = [e for e in items_in_folder if e.get("type") != "vod_category"]

        # Series detection
        is_series = bool(vod_items) and all(
            e.get("season_number") is not None for e in vod_items
        )

        # Ensure target directory
        ensure_directory(fs_dir)
        self._folder_count += 1

        # Write tvshow.nfo and folder poster for series roots
        if is_series:
            # Commented out – .nfo and poster.jpg sourced from database instead
            # tvshow_nfo = os.path.join(fs_dir, "tvshow.nfo")
            # atomic_write(tvshow_nfo, _nfo_tvshow(
            #     category_name, category_desc, category_logo
            # ))
            # self._expected.add(tvshow_nfo)
            # _log(f"tvshow.nfo → {tvshow_nfo}", xbmc.LOGINFO)

            # if category_logo:
            #     poster_path = os.path.join(fs_dir, "poster.jpg")
            #     if download_image(category_logo, poster_path):
            #         self._expected.add(poster_path)
            pass

        # Write playable VOD items
        for entry in vod_items:
            self._check_abort()
            self._write_item(entry, fs_dir, is_series)

        # Recurse into sub-categories
        for cat_entry in sub_cats:
            self._check_abort()
            child_content_id = cat_entry.get("id", "")
            child_name = sanitise_filename(cat_entry.get("name", child_content_id))
            child_dir = os.path.join(fs_dir, child_name)

            msg = f"Exporting: {child_name}"
            self._progress.update(-1, message=msg)

            self._crawl(child_content_id, child_dir, depth + 1)

    def _write_item(self, entry, fs_dir, in_series):
        """Write a single playable item (.strm and .nfo)."""
        item_id = entry.get("Id", "")
        name = entry.get("Name", item_id)
        content_t = entry.get("ContentType", "VOD")
        season = entry.get("season_number")
        episode = entry.get("episode_number")
        year = entry.get("release_year")
        logo = safe_image_url(entry.get("LogoUrl", ""))

        # Determine file base name and target directory
        if in_series and season is not None and episode is not None:
            # Season sub-directory
            season_dir = os.path.join(fs_dir, f"Season {int(season):02d}")
            ensure_directory(season_dir)
            show_title = sanitise_filename(
                entry.get("SeriesTitle") or os.path.basename(fs_dir)
            )
            base_name = sanitise_filename(
                f"{show_title} S{int(season):02d}E{int(episode):02d}"
            )
            target_dir = season_dir
        elif content_t == "MOVIE":
            year_str = f" ({int(year)})" if year else ""
            base_name = sanitise_filename(f"{name}{year_str}")
            target_dir = fs_dir
        else:
            base_name = sanitise_filename(name)
            target_dir = fs_dir

        strm_path = os.path.join(target_dir, base_name + ".strm")
        nfo_path = os.path.join(target_dir, base_name + ".nfo")

        # Write .strm
        atomic_write(strm_path, _build_plugin_url(self._provider, item_id))
        self._expected.add(strm_path)
        self._item_count += 1

        # Write .nfo
        # Commented out – .nfo sourced from database instead
        # if in_series and season is not None:
        #     nfo_content = _nfo_episode(entry)
        # else:
        #     nfo_content = _nfo_movie(entry)
        #
        # atomic_write(nfo_path, nfo_content)
        # self._expected.add(nfo_path)

        # Poster
        # Commented out – poster.jpg sourced from database instead
        # if logo:
        #     poster_path = os.path.join(target_dir, "poster.jpg")
        #     if download_image(logo, poster_path):
        #         self._expected.add(poster_path)

        # Season poster (write once per season dir)
        # Commented out – poster.jpg sourced from database instead
        # if in_series and season is not None:
        #     season_poster = os.path.join(season_dir, "poster.jpg")
        #     if logo and not os.path.exists(season_poster):
        #         if download_image(logo, season_poster):
        #             self._expected.add(season_poster)

        _log(f"Written: {os.path.basename(strm_path)} → {target_dir}", xbmc.LOGDEBUG)

        # Progress update
        self._progress.update(-1, message=f"Items: {self._item_count} – {name}")

    def _check_abort(self):
        if self._progress.is_aborted():
            raise _AbortRequested()


class _AbortRequested(Exception):
    pass


def _run_export(provider, content_id, folder_name):
    """Background thread function for VOD export."""
    display_name = sanitise_filename(folder_name or content_id)

    progress = ExportProgress(
        "Ultimate – Export VOD",
        f"Preparing export of '{display_name}'…"
    )

    try:
        if progress.is_aborted():
            return

        # Determine target directory: <root>/library/<provider_safe>/vod/<folder_name>/
        target_dir = ensure_export_path(
            EXPORT_TYPE_LIBRARY,
            provider,
            ["vod", display_name]
        )
        _log(f"Export target: {target_dir}", xbmc.LOGINFO)

        client = get_client_from_settings()
        job = _ExportJob(client, provider, content_id, target_dir, progress)
        items, folders = job.run()

        if items or folders:
            notify(
                f"Export complete: {items} items in {folders} folder(s).",
                icon=xbmcgui.NOTIFICATION_INFO,
            )
        else:
            notify("Export finished — no items found.")

    except Exception as exc:
        _log(f"Unexpected error: {exc}", xbmc.LOGERROR)
        notify_error(f"Export failed unexpectedly: {exc}")
    finally:
        progress.close()


def export_vod_library(provider, content_id, folder_name=""):
    """
    Launch the VOD export for content_id under provider in a background thread.

    Output directory: <export_root>/library/<provider_safe>/vod/<folder_name_safe>/

    Parameters
    ----------
    provider    : provider name (e.g. "rtlplus")
    content_id  : opaque category ID as returned by the API (e.g. "folder_4")
    folder_name : human-readable category name used to name the export directory
    """
    if not provider or not content_id:
        notify_error("Export: missing provider or content ID.")
        return

    run_in_background(_run_export, "vod_export", provider, content_id, folder_name)