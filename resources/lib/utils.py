#!/usr/bin/env python3
"""
General utility helpers for the Ultimate addon.
"""

import xbmc
import xbmcaddon
import xbmcgui

ADDON = xbmcaddon.Addon()


def get_setting(key):
    return ADDON.getSetting(key)


def get_setting_bool(key):
    # getSetting always returns a string; compare explicitly to avoid type errors
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
