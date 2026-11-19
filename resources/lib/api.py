# resources/lib/api.py
import json
import urllib.request
import urllib.parse
from urllib.error import URLError, HTTPError
import xbmc
import xbmcaddon

class APIError(Exception):
    """Base exception for API errors."""
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code

class APIAuthPendingError(APIError):
    """Exception raised when authentication is pending (HTTP 202)."""
    def __init__(self, message="Login pending", login_code="", qr_target_url="", remaining_seconds=300):
        super().__init__(message, status_code=202)
        self.login_code = login_code
        self.qr_target_url = qr_target_url
        self.remaining_seconds = remaining_seconds

class UltimateBackendClient:
    def __init__(self, host=None, port=None):
        addon = xbmcaddon.Addon()
        self.host = host or addon.getSetting("server_ip") or "127.0.0.1"
        self.port = port or addon.getSetting("server_port") or "7777"
        self.base_url = f"http://{self.host}:{self.port}"
        self.timeout = 10

    def _get(self, path, params=None):
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Kodi-Ultimate-Plugin/1.3.13"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status_code = getattr(resp, 'status', getattr(resp, 'code', 200))
                body = resp.read().decode('utf-8')
                data = json.loads(body)
                if status_code == 202 or (isinstance(data, dict) and (data.get("status") == "login_pending" or data.get("error") == "auth_pending")):
                    raise APIAuthPendingError(
                        message=data.get("message", "Login pending"),
                        login_code=data.get("login_code", ""),
                        qr_target_url=data.get("qr_target_url", data.get("qr_code_url", "")),
                        remaining_seconds=data.get("remaining_seconds", 300)
                    )
                return data
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                err_obj = json.loads(body)
                msg = err_obj.get("error") or err_obj.get("message") or str(e)
            except Exception:
                msg = str(e)
            raise APIError(msg, status_code=e.code)
        except URLError as e:
            raise APIError(f"Connection failed: {e.reason}")

    def get_providers(self):
        data = self._get("/api/providers")
        if isinstance(data, dict):
            return data.get("providers", [])
        return []

    def get_channels(self, provider, country=None):
        params = {}
        if country:
            params["country"] = country
        data = self._get(f"/api/providers/{provider}/channels", params=params)
        if isinstance(data, dict) and "channels" in data:
            return data["channels"]
        return data
