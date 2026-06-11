#!/usr/bin/env python3
"""
API client for Ultimate Backend server.

Field name reference (from server-side to_dict() methods):
  Channel/Content keys  : Name, Id, Provider, LogoUrl, Quality, Mode,
                          ContentType, Language, Country, ChannelNumber,
                          IsRadio, CatchupHours, DrmConfig, SessionManifest, ...
  Event extra keys      : StartTime (ISO 8601), EndTime (ISO 8601),
                          Status, DurationMinutes
  VOD entry keys        : type ("vod_category" | "vod"), id, name, slug,
                          provider, logo_url, description, child_count,
                          duration_seconds, duration_minutes, release_year,
                          rating, cast, director, season_number, episode_number
  DRM config structure  : drm_configs -> { "com.widevine.alpha": {
                            "priority": int,
                            "license": { "server_url", "req_headers",
                                         "req_params", "req_data",
                                         "wrapper", "unwrapper", ... }
                          } }

Manifest responses may include:
  x-kodi-drm-configs    — DRM config JSON (base64). Preferred over /drm endpoint.
  x-kodi-stream-headers — manifest/segment HTTP headers JSON (base64).
                          Payload: {"manifest": {...}, "segment": {...}}
urllib follows 302 redirects automatically; headers are read from the final
response, so the header will be captured even after a redirect.
"""

import json
import time
from base64 import b64decode
from urllib.request import urlopen, Request, HTTPRedirectHandler, build_opener
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

import xbmc

# ---------------------------------------------------------------------------
# Simple in-memory cache
# ---------------------------------------------------------------------------
_cache = {}  # key -> (value, expires_at)


def _cache_get(key):
    entry = _cache.get(key)
    if entry and time.time() < entry[1]:
        return entry[0]
    return None


def _cache_set(key, value, ttl):
    _cache[key] = (value, time.time() + ttl)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class APIError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class UltimateBackendClient:
    """HTTP client for the Ultimate Backend REST API."""

    PROVIDER_TTL  = 300   # 5 min
    CHANNEL_TTL   = 300   # 5 min
    EVENT_TTL     = 60    # 1 min (events change frequently)
    VOD_TTL       = 600   # 10 min
    FAVORITES_TTL = 120   # 2 min (user may add/remove between visits)

    def __init__(self, host, port, use_https=False, timeout=10):
        # Basic validation to prevent URL injection via malformed host setting
        host = host.strip().strip("/").split("/")[0].split("@")[-1]
        scheme = "https" if use_https else "http"
        self.base_url = f"{scheme}://{host}:{port}"
        self.timeout = timeout

    def _url(self, path):
        return self.base_url + path

    def _get(self, path, params=None):
        url = self._url(path)
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = url + "?" + urlencode(clean)
        xbmc.log(f"[Ultimate] GET {url}", xbmc.LOGDEBUG)
        try:
            req = Request(url)
            req.add_header("Accept", "application/json")
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw)
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                err_obj = json.loads(body)
                msg = err_obj.get("error", str(e))
            except Exception:
                msg = str(e)
            raise APIError(msg, status_code=e.code)
        except URLError as e:
            raise APIError(f"Connection failed: {e.reason}")

    def _get_manifest_with_drm(self, path, params=None):
        """
        Fetch a manifest endpoint and capture x-kodi-drm-configs and
        x-kodi-stream-headers response headers.

        The headers are only ever present on the *first* response (whether that
        is a 302/303 redirect or a direct 200). Strategy:

          1. Make the first request with redirect-following disabled so we can
             read the headers before urllib discards them.
          2. Extract x-kodi-drm-configs and x-kodi-stream-headers from that
             first response.
          3. If the first response was a redirect, follow the Location URL
             normally (with urllib's default redirect handling) to get the
             manifest body. If it was a 200 already, read the body directly.

        Returns:
            (manifest_data: dict, drm_configs: dict | None,
             stream_headers: dict | None)

        drm_configs is None when the header was absent — the caller should
        fall back to the /drm endpoint in that case.
        stream_headers has the shape {"manifest": {...}, "segment": {...}} when
        present, or None when the header was absent.
        """
        url = self._url(path)
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = url + "?" + urlencode(clean)

        xbmc.log(f"[Ultimate] GET manifest {url}", xbmc.LOGDEBUG)

        drm_configs    = None
        stream_headers = None
        manifest_data  = {}

        class _NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None  # suppress automatic redirect following

        no_redirect_opener = build_opener(_NoRedirect())

        try:
            req = Request(url)
            req.add_header("Accept", "application/json")

            try:
                with no_redirect_opener.open(req, timeout=self.timeout) as first_resp:
                    first_status  = first_resp.status
                    first_headers = first_resp.headers

                    # Always try to read the DRM header from the first response
                    drm_header = first_headers.get("x-kodi-drm-configs")
                    if drm_header:
                        try:
                            decoded = b64decode(drm_header).decode("utf-8")
                            drm_configs = json.loads(decoded)
                            xbmc.log(
                                f"[Ultimate] x-kodi-drm-configs OK on HTTP {first_status} "
                                f"for {path} — systems: {list(drm_configs.keys())}",
                                xbmc.LOGINFO,
                            )
                        except Exception as parse_err:
                            xbmc.log(
                                f"[Ultimate] x-kodi-drm-configs present on HTTP {first_status} "
                                f"for {path} but base64/JSON decode failed: {parse_err}",
                                xbmc.LOGWARNING,
                            )
                    else:
                        xbmc.log(
                            f"[Ultimate] x-kodi-drm-configs absent on HTTP {first_status} "
                            f"for {path} — will fall back to /drm endpoint",
                            xbmc.LOGDEBUG,
                        )

                    # Extract x-kodi-stream-headers from the first response
                    sh_header = first_headers.get("x-kodi-stream-headers")
                    if sh_header:
                        try:
                            decoded_sh = b64decode(sh_header).decode("utf-8")
                            stream_headers = json.loads(decoded_sh)
                            xbmc.log(
                                f"[Ultimate] x-kodi-stream-headers OK on HTTP {first_status} "
                                f"for {path}",
                                xbmc.LOGINFO,
                            )
                        except Exception as parse_err:
                            xbmc.log(
                                f"[Ultimate] x-kodi-stream-headers present on HTTP {first_status} "
                                f"for {path} but base64/JSON decode failed: {parse_err}",
                                xbmc.LOGWARNING,
                            )
                    else:
                        xbmc.log(
                            f"[Ultimate] x-kodi-stream-headers absent on HTTP {first_status} "
                            f"for {path}",
                            xbmc.LOGDEBUG,
                        )

                    if first_status in (301, 302, 303, 307, 308):
                        location = first_headers.get("Location")
                        if not location:
                            raise APIError(
                                f"Redirect HTTP {first_status} with no Location for {path}"
                            )
                        xbmc.log(
                            f"[Ultimate] Redirect HTTP {first_status} → {location}, "
                            f"following for manifest body",
                            xbmc.LOGDEBUG,
                        )
                        # Follow the redirect normally from here — no more
                        # header inspection needed, just get the body
                        follow_req = Request(location)
                        follow_req.add_header("Accept", "application/json")
                        with urlopen(follow_req, timeout=self.timeout) as final_resp:
                            raw = final_resp.read()
                    else:
                        # First response was already the final one
                        raw = first_resp.read()

            except HTTPError as e:
                # The no-redirect opener raises HTTPError for actual errors
                # (4xx/5xx). Still try to salvage the DRM header.
                drm_header = e.headers.get("x-kodi-drm-configs") if e.headers else None
                if drm_header:
                    try:
                        decoded = b64decode(drm_header).decode("utf-8")
                        drm_configs = json.loads(decoded)
                        xbmc.log(
                            f"[Ultimate] x-kodi-drm-configs recovered from HTTP {e.code} "
                            f"error for {path} — systems: {list(drm_configs.keys())}",
                            xbmc.LOGWARNING,
                        )
                    except Exception:
                        xbmc.log(
                            f"[Ultimate] x-kodi-drm-configs present on HTTP {e.code} "
                            f"error for {path} but base64/JSON decode failed",
                            xbmc.LOGWARNING,
                        )
                sh_header_err = e.headers.get("x-kodi-stream-headers") if e.headers else None
                if sh_header_err:
                    try:
                        decoded_sh = b64decode(sh_header_err).decode("utf-8")
                        stream_headers = json.loads(decoded_sh)
                        xbmc.log(
                            f"[Ultimate] x-kodi-stream-headers recovered from HTTP {e.code} "
                            f"error for {path}",
                            xbmc.LOGWARNING,
                        )
                    except Exception:
                        xbmc.log(
                            f"[Ultimate] x-kodi-stream-headers present on HTTP {e.code} "
                            f"error for {path} but base64/JSON decode failed",
                            xbmc.LOGWARNING,
                        )
                body = e.read().decode("utf-8", errors="replace")
                try:
                    msg = json.loads(body).get("error", str(e))
                except Exception:
                    msg = str(e)
                raise APIError(msg, status_code=e.code)

            try:
                manifest_data = json.loads(raw)
            except Exception:
                manifest_data = {}

        except URLError as e:
            raise APIError(f"Connection failed: {e.reason}")

        return manifest_data, drm_configs, stream_headers

    # ------------------------------------------------------------------
    # Providers
    # ------------------------------------------------------------------

    def get_providers(self):
        cached = _cache_get("providers")
        if cached is not None:
            return cached
        data = self._get("/api/providers")
        result = data.get("providers", [])
        _cache_set("providers", result, self.PROVIDER_TTL)
        return result

    def get_all_providers(self):
        data = self._get("/api/providers")
        return data.get("all_providers", [])

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    def get_channels(self, provider, country=None):
        cache_key = f"channels:{provider}:{country}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
        data = self._get(f"/api/providers/{provider}/channels",
                         params={"country": country})
        result = data.get("channels", [])
        _cache_set(cache_key, result, self.CHANNEL_TTL)
        return result

    def get_channel_manifest(self, provider, channel_id, country=None):
        """
        GET /api/providers/{provider}/channels/{channel_id}/manifest
        Returns (manifest_data, drm_configs, stream_headers, catchup_template)
        """
        params = {}
        if country:
            params["country"] = country
        # No start_time/end_time - they belong on stream endpoint

        manifest_data, drm_configs, stream_headers = self._get_manifest_with_drm(
            f"/api/providers/{provider}/channels/{channel_id}/manifest",
            params=params or None,
        )

        catchup_template = manifest_data.get("catchup_stream_url_template")
        return manifest_data, drm_configs, stream_headers, catchup_template

    def get_channel_stream_url(self, provider, channel_id, country=None):
        return self._url(
            f"/api/providers/{provider}/channels/{channel_id}/stream/index.mpd"
        )

    def get_channel_drm(self, provider, channel_id, country=None,
                        start_time=None, end_time=None):
        params = {}
        if country:
            params["country"] = country
        if start_time is not None:
            params["start_time"] = start_time
        if end_time is not None:
            params["end_time"] = end_time
        return self._get(
            f"/api/providers/{provider}/channels/{channel_id}/drm",
            params=params or None,
        )

    def get_channel_epg(self, provider, channel_id, country=None):
        try:
            data = self._get(f"/api/providers/{provider}/channels/{channel_id}/epg",
                             params={"country": country})
            return data.get("epg", [])
        except APIError:
            return []

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def get_events(self, provider, start_time=None, end_time=None):
        cache_key = f"events:{provider}:{start_time}:{end_time}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
        params = {"start_time": start_time, "end_time": end_time}
        data = self._get(f"/api/providers/{provider}/events", params=params)
        result = data.get("events", [])
        _cache_set(cache_key, result, self.EVENT_TTL)
        return result

    def get_event_manifest(self, provider, event_id, country=None):
        """
        GET /api/providers/{provider}/events/{event_id}/manifest
        Returns (manifest_data, drm_configs_from_header_or_None,
                 stream_headers_from_header_or_None).
        """
        return self._get_manifest_with_drm(
            f"/api/providers/{provider}/events/{event_id}/manifest",
            params={"country": country},
        )

    def get_event_stream_url(self, provider, event_id):
        return self._url(
            f"/api/providers/{provider}/events/{event_id}/stream/index.mpd"
        )

    def get_event_drm(self, provider, event_id, country=None):
        return self._get(f"/api/providers/{provider}/events/{event_id}/drm",
                         params={"country": country})

    # ------------------------------------------------------------------
    # VOD
    # ------------------------------------------------------------------

    def get_vod_root(self, provider, cursor=None, page_size=None):
        """
        Fetch the root VOD listing.

        Returns a dict with at least:
            {
                "entries":     [...],
                "next_cursor": "<opaque>" | None,
                "total":       int | None,
            }

        Results are cached only for the first page (cursor=None) so that
        subsequent pages always hit the server.
        """
        params = {}
        if cursor:
            params["cursor"] = cursor
        if page_size is not None:
            params["size"] = page_size

        # Only cache the very first page
        if not cursor:
            cache_key = f"vod_root:{provider}:{page_size}"
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached

        data = self._get(f"/api/providers/{provider}/vod", params=params or None)
        result = {
            "entries":     data.get("entries", []),
            "next_cursor": data.get("next_cursor"),
            "total":       data.get("total"),
        }
        if not cursor:
            _cache_set(cache_key, result, self.VOD_TTL)
        return result

    def get_vod_path(self, provider, path, cursor=None, page_size=None):
        """
        Fetch a VOD sub-tree node.

        Returns a dict with at least:
            {
                "entries":     [...],
                "next_cursor": "<opaque>" | None,
                "total":       int | None,
            }

        Results are cached only for the first page (cursor=None).
        """
        params = {}
        if cursor:
            params["cursor"] = cursor
        if page_size is not None:
            params["size"] = page_size

        if not cursor:
            cache_key = f"vod_path:{provider}:{path}:{page_size}"
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached

        data = self._get(f"/api/providers/{provider}/vod/{path}",
                         params=params or None)
        result = {
            "entries":     data.get("entries", []),
            "next_cursor": data.get("next_cursor"),
            "total":       data.get("total"),
        }
        if not cursor:
            _cache_set(cache_key, result, self.VOD_TTL)
        return result

    def get_vod_manifest_by_id(self, provider, vod_id, country=None):
        """
        Fetch manifest for a VOD item using its content_id directly.

        Args:
            provider: Provider name
            vod_id: The content_id from the VodItem entry
            country: Optional country code

        Returns:
            (manifest_data, drm_configs, stream_headers)
        """
        params = {"country": country} if country else None
        return self._get_manifest_with_drm(
            f"/api/providers/{provider}/vod/{vod_id}/manifest",
            params=params
        )

    def get_vod_manifest(self, provider, vod_path):
        """
        GET /api/providers/{provider}/vod/{full_path}/manifest
        Returns (manifest_data, drm_configs_from_header_or_None,
                 stream_headers_from_header_or_None).
        manifest_data["manifest_url"] is the actual stream URL to use.
        """
        return self._get_manifest_with_drm(
            f"/api/providers/{provider}/vod/{vod_path}/manifest"
        )

    def get_vod_stream_url(self, provider, vod_path):
        """
        vod_path must be the full path including category prefix, e.g.:
          "sports/alpine-skiing/705e62b0-6241-4520-8610-6c673b3d9818"
        """
        return self._url(
            f"/api/providers/{provider}/vod/{vod_path}/stream/index.mpd"
        )

    def get_vod_drm(self, provider, vod_path):
        """vod_path must be the full path, e.g. "sports/alpine-skiing/705e62b0-..." """
        return self._get(f"/api/providers/{provider}/vod/{vod_path}/drm")

    def search_vod(self, provider, query):
        """Search VOD content. Returns list of matching vod entries."""
        try:
            data = self._get(f"/api/providers/{provider}/vod/search",
                             params={"q": query})
            return data.get("entries", [])
        except APIError:
            return []

    # ------------------------------------------------------------------
    # Recordings
    # ------------------------------------------------------------------

    def get_recordings(self, provider, include_deleted=False):
        """Return list of recording dicts for *provider*."""
        params = {"include_deleted": "true" if include_deleted else "false"}
        data = self._get(f"/api/providers/{provider}/recordings", params=params)
        return data.get("recordings", [])

    def get_recording_manifest(self, provider, recording_id):
        """Return (manifest_data, drm_from_header) for *recording_id*."""
        return self._get_manifest_with_drm(
            f"/api/providers/{provider}/recordings/{recording_id}/manifest"
        )

    def get_recording_drm(self, provider, recording_id):
        """Return raw DRM config dict for *recording_id*."""
        return self._get(
            f"/api/providers/{provider}/recordings/{recording_id}/drm"
        )

    # ------------------------------------------------------------------
    # Favorites
    # ------------------------------------------------------------------

    def get_favorites(self, provider, favorite_type=None):
        """
        Fetch favorites for a provider.

        Args:
            provider:      Provider name (e.g., "rtlplus")
            favorite_type: Optional filter — e.g. "PROGRAM", "MOVIE", "CHANNEL"

        Returns:
            dict with keys: "provider", "favorites", "count", "filters"
            Each favorite contains: FavoriteId, Provider, ContentId,
            FavoriteType, CreatedAt, Title, ThumbnailUrl, SeriesTitle
        """
        cache_key = f"favorites:{provider}:{favorite_type}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        params = {}
        if favorite_type:
            params["favorite_type"] = favorite_type

        data = self._get(
            f"/api/providers/{provider}/favorites",
            params=params if params else None,
        )
        _cache_set(cache_key, data, self.FAVORITES_TTL)
        return data

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def ping(self):
        try:
            self._get("/api/providers")
            return True
        except Exception:
            return False