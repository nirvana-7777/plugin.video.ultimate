#!/usr/bin/env python3
"""
General utility helpers for the Ultimate addon.
Includes shared export functionality.
"""

import os
import re
import threading
from urllib.request import urlretrieve
from urllib.error import URLError

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon()


# ---------------------------------------------------------------------------
# Basic settings helpers
# ---------------------------------------------------------------------------

def get_setting(key):
    return ADDON.getSetting(key)


def get_setting_bool(key):
    return ADDON.getSetting(key).lower() == "true"


def get_setting_int(key, default=0):
    try:
        return int(ADDON.getSetting(key))
    except (ValueError, TypeError):
        return default


def notify(message, heading="Ultimate", icon=xbmcgui.NOTIFICATION_INFO, time=4000):
    xbmcgui.Dialog().notification(heading, message, icon, time)


def notify_error(message):
    notify(message, icon=xbmcgui.NOTIFICATION_ERROR, time=6000)


def safe_image_url(url):
    if url and url.startswith("http"):
        return url
    return ""


def provider_label(provider_data):
    return provider_data.get("label") or provider_data.get("name", "Unknown")


# ---------------------------------------------------------------------------
# Export constants and path management
# ---------------------------------------------------------------------------

EXPORT_TYPE_PLAYLISTS = "playlists"
EXPORT_TYPE_LIBRARY = "library"
EXPORT_TYPE_METADATA = "metadata"


def resolve_export_root():
    """
    Return the absolute export root path from the addon setting, or a safe
    default inside Kodi's own userdata directory.
    """
    from_setting = get_setting("export_root_path") or ""
    if from_setting:
        return xbmcvfs.translatePath(from_setting.rstrip("/\\"))
    default = "special://home/ultimate"
    return xbmcvfs.translatePath(default)


def sanitise_filename(name):
    """Replace filesystem-unsafe characters with an underscore."""
    if not name:
        return "unknown"
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def ensure_directory(path):
    """Create directory if it doesn't exist (using xbmcvfs)."""
    if not xbmcvfs.exists(path):
        success = xbmcvfs.mkdirs(path)
        if not success:
            # mkdirs returns False on some Kodi builds if dir already exists
            if not xbmcvfs.exists(path):
                return False
    return True


def get_export_path(export_type, provider, subpath=None):
    """
    Get unified export path.

    Args:
        export_type: One of EXPORT_TYPE_* constants
        provider: Provider name
        subpath: Optional additional path components (string or list/tuple)

    Returns:
        Absolute filesystem path
    """
    root = resolve_export_root()
    provider_safe = sanitise_filename(provider)

    path_parts = [root, export_type, provider_safe]
    if subpath:
        if isinstance(subpath, (list, tuple)):
            path_parts.extend(subpath)
        else:
            path_parts.append(subpath)

    return os.path.join(*path_parts)


def ensure_export_path(export_type, provider, subpath=None):
    """Create and return export path."""
    path = get_export_path(export_type, provider, subpath)
    ensure_directory(path)
    return path


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

def get_client_from_settings():
    """Create a backend client from addon settings."""
    from api import UltimateBackendClient

    ip = get_setting("server_ip") or "localhost"
    raw_port = get_setting("server_port") or "8000"
    try:
        port = int(raw_port)
    except (ValueError, TypeError):
        port = 8000
    use_https = get_setting_bool("use_https")
    return UltimateBackendClient(ip, port, use_https=use_https)


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

def atomic_write(path, content):
    """
    Write content atomically using a temporary file.

    Uses xbmcvfs for Kodi path compatibility.
    """
    tmp_path = path + ".tmp"

    # Write to temp file
    f = xbmcvfs.File(tmp_path, "w")
    try:
        if not f.write(content):
            raise IOError(f"xbmcvfs.File.write() returned False for {tmp_path}")
    finally:
        f.close()

    # Atomic rename
    if not xbmcvfs.rename(tmp_path, path):
        # Fall back to delete+rename on platforms where rename-over-existing
        # is not supported
        xbmcvfs.delete(path)
        if not xbmcvfs.rename(tmp_path, path):
            raise IOError(f"Could not rename {tmp_path} → {path}")

    return True


def download_image(url, dest_path, overwrite=False):
    """
    Download image only if not exists (or overwrite=True).

    Returns True if download was successful or file already exists,
    False on error.
    """
    if not overwrite and xbmcvfs.exists(dest_path):
        return True

    try:
        # Download to temp file first
        tmp_path = dest_path + ".tmp"
        urlretrieve(url, tmp_path)

        # Atomic move to destination
        if xbmcvfs.exists(dest_path):
            xbmcvfs.delete(dest_path)
        if not xbmcvfs.rename(tmp_path, dest_path):
            xbmcvfs.delete(tmp_path)
            return False

        xbmc.log(f"[Ultimate] Image saved: {dest_path}", xbmc.LOGINFO)
        return True
    except (URLError, OSError, IOError) as exc:
        xbmc.log(f"[Ultimate] Image download failed ({url}): {exc}", xbmc.LOGWARNING)
        # Remove partial download if present
        tmp = dest_path + ".tmp"
        if xbmcvfs.exists(tmp):
            xbmcvfs.delete(tmp)
        return False


# ---------------------------------------------------------------------------
# Progress dialog helper
# ---------------------------------------------------------------------------

class ExportProgress:
    """Background progress dialog with abort checking."""

    def __init__(self, title, message):
        self.dialog = xbmcgui.DialogProgressBG()
        self.dialog.create(title, message)
        self.monitor = xbmc.Monitor()
        self._closed = False

    def update(self, percent, message=None):
        """Update progress (percent -1 for indeterminate)."""
        if self._closed:
            return
        if not self.monitor.abortRequested():
            if message:
                self.dialog.update(percent, message=message)
            else:
                self.dialog.update(percent)

    def close(self):
        """Close the progress dialog."""
        if not self._closed:
            self.dialog.close()
            self._closed = True

    def is_aborted(self):
        """Check if Kodi is shutting down or user requested abort."""
        return self.monitor.abortRequested()


# ---------------------------------------------------------------------------
# Background thread runner
# ---------------------------------------------------------------------------

def run_in_background(target, name, *args, **kwargs):
    """
    Run a function in a daemon background thread.

    Returns the thread object.
    """
    thread = threading.Thread(target=target, name=name, args=args, kwargs=kwargs, daemon=True)
    thread.start()
    return thread