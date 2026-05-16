#!/usr/bin/env python3
"""
DRM helper — Kodi 22+ / InputStream Adaptive utilities.

The main DRM configuration logic now lives in addon.py (_normalize_drm_configs,
_configure_playback) using the new `inputstream.adaptive.drm` JSON property.
This module provides only the Widevine CDM availability check.
"""

import xbmc
import xbmcaddon


def check_inputstream_adaptive():
    """Return True if inputstream.adaptive is available."""
    try:
        xbmcaddon.Addon("inputstream.adaptive")
        return True
    except Exception:
        return False


def ensure_widevine():
    """
    Use inputstreamhelper (if available) to ensure Widevine CDM is installed.
    Returns True if ready, False otherwise.
    """
    try:
        import inputstreamhelper
        helper = inputstreamhelper.Helper("mpd", drm="com.widevine.alpha")
        return helper.check_inputstream()
    except ImportError:
        xbmc.log("[Ultimate] inputstreamhelper not available — skipping Widevine check", xbmc.LOGWARNING)
        return True  # Assume OK; ISA will surface the error if CDM is missing
    except Exception as e:
        xbmc.log(f"[Ultimate] Widevine check failed: {e}", xbmc.LOGWARNING)
        return False
