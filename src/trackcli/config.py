"""Configuration: optional official-API keys and tunables.

Resolution order (later wins for scalars):

1. Built-in defaults,
2. ``$XDG_CONFIG_HOME/track/config.json`` (default ``~/.config/track``)::

       {
         "ttl_seconds": 300,
         "allow_browser": true,
         "usps":  {"user_id": "XXXX", "client_id": "...", "client_secret": "..."},
         "dhl":   {"api_key": "..."},
         "fedex": {"api_key": "...", "api_secret": "..."}
       }

3. Environment variables (handy for agents/CI)::

       TRACK_TTL            cache seconds       (default 300)
       TRACK_NO_BROWSER     any value           -> never use playwright
       TRACK_USPS_USERID    legacy XML API key
       TRACK_USPS_CLIENT_ID / TRACK_USPS_CLIENT_SECRET   (api.usps.com OAuth2)
       TRACK_DHL_API_KEY    developer.dhl.com free key
       TRACK_FEDEX_API_KEY / TRACK_FEDEX_API_SECRET

None of these are required -- the keyless chain (AfterShip ->
PackageRadar) works with zero configuration.  Keys only *upgrade* the
quality of carrier-direct data and are tried before the keyless chain.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


def _config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "track" / "config.json"


@dataclass
class Config:
    ttl_seconds: int = 300
    timeout: int = 20                 # per-provider network timeout (s)
    allow_browser: bool = True        # tier-3 playwright last resort
    usps_user_id: str = ""            # legacy ShippingAPI.dll TrackV2
    usps_client_id: str = ""          # api.usps.com OAuth2 (new platform)
    usps_client_secret: str = ""
    dhl_api_key: str = ""             # api-eu.dhl.com
    fedex_api_key: str = ""           # api.fedex.com/track/v3 (OAuth2)
    fedex_api_secret: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def has_usps_legacy(self) -> bool:
        return bool(self.usps_user_id)

    @property
    def has_usps_oauth(self) -> bool:
        return bool(self.usps_client_id and self.usps_client_secret)

    @property
    def has_dhl(self) -> bool:
        return bool(self.dhl_api_key)

    @property
    def has_fedex(self) -> bool:
        return bool(self.fedex_api_key and self.fedex_api_secret)


def load() -> Config:
    cfg = Config()

    # --- JSON file (missing file is perfectly fine) ---------------------
    path = _config_path()
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        blob = {}
    if isinstance(blob, dict):
        cfg.extra = blob
        cfg.ttl_seconds = int(blob.get("ttl_seconds", cfg.ttl_seconds))
        cfg.timeout = int(blob.get("timeout", cfg.timeout))
        cfg.allow_browser = bool(blob.get("allow_browser", cfg.allow_browser))
        for section, attr in (("usps", None), ("dhl", None), ("fedex", None)):
            values = blob.get(section, {})
            if not isinstance(values, dict):
                continue
            if section == "usps":
                cfg.usps_user_id = values.get("user_id", "")
                cfg.usps_client_id = values.get("client_id", "")
                cfg.usps_client_secret = values.get("client_secret", "")
            elif section == "dhl":
                cfg.dhl_api_key = values.get("api_key", "")
            elif section == "fedex":
                cfg.fedex_api_key = values.get("api_key", "")
                cfg.fedex_api_secret = values.get("api_secret", "")

    # --- environment overrides ------------------------------------------
    def env(name: str, current: str) -> str:
        return os.environ.get(name, current) or current

    cfg.ttl_seconds = int(os.environ.get("TRACK_TTL", cfg.ttl_seconds))
    if os.environ.get("TRACK_NO_BROWSER"):
        cfg.allow_browser = False
    cfg.usps_user_id = env("TRACK_USPS_USERID", cfg.usps_user_id)
    cfg.usps_client_id = env("TRACK_USPS_CLIENT_ID", cfg.usps_client_id)
    cfg.usps_client_secret = env("TRACK_USPS_CLIENT_SECRET",
                                 cfg.usps_client_secret)
    cfg.dhl_api_key = env("TRACK_DHL_API_KEY", cfg.dhl_api_key)
    cfg.fedex_api_key = env("TRACK_FEDEX_API_KEY", cfg.fedex_api_key)
    cfg.fedex_api_secret = env("TRACK_FEDEX_API_SECRET", cfg.fedex_api_secret)

    if cfg.ttl_seconds < 0:
        cfg.ttl_seconds = 0
    return cfg


def example_config() -> str:
    """A ready-to-edit config.json body printed by `track config`."""
    return json.dumps({
        "ttl_seconds": 300,
        "allow_browser": True,
        "usps": {"user_id": "YOUR-USPS-WEB-TOOLS-USERID"},
        "dhl": {"api_key": "YOUR-DHL-DEVELOPER-KEY"},
        "fedex": {"api_key": "...", "api_secret": "..."},
    }, indent=2)
