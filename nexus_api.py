"""Nexus Mods API client for looking up mod update information."""

import re
from datetime import datetime, timezone
from typing import Optional

import requests

NEXUS_API_BASE = "https://api.nexusmods.com/v1"
NEXUS_WEB_BASE = "https://www.nexusmods.com"


class NexusAPIError(Exception):
    """Raised when a Nexus API call fails."""


class NexusAPI:
    """Thin wrapper around the Nexus Mods v1 REST API."""

    def __init__(self, api_key: str, game_domain: str = "baldursgate3"):
        self.api_key = api_key
        self.game_domain = game_domain
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": self.api_key,
                "accept": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, **kwargs) -> dict:
        url = f"{NEXUS_API_BASE}{path}"
        try:
            resp = self.session.get(url, timeout=15, **kwargs)
        except requests.RequestException as exc:
            raise NexusAPIError(f"Network error: {exc}") from exc
        if resp.status_code == 401:
            raise NexusAPIError("Invalid API key. Check your Nexus Mods API key.")
        if resp.status_code == 429:
            raise NexusAPIError("Rate-limited by Nexus Mods. Wait a moment and try again.")
        if resp.status_code != 200:
            raise NexusAPIError(f"API error {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def validate_key(self) -> dict:
        """Validate the API key and return user info."""
        return self._get("/users/validate.json")

    def get_mod_info(self, mod_id: int) -> dict:
        """Return full mod information for *mod_id*."""
        return self._get(f"/games/{self.game_domain}/mods/{mod_id}.json")

    def get_mod_files(self, mod_id: int) -> list[dict]:
        """Return the list of files for *mod_id*."""
        data = self._get(f"/games/{self.game_domain}/mods/{mod_id}/files.json")
        return data.get("files", [])

    def get_download_links(self, mod_id: int, file_id: int) -> list[dict]:
        """Return download links for a specific file.

        Each entry has 'name' (mirror label) and 'URI' (download URL).
        Requires a Premium API key for direct download; free keys get
        only the Nexus web-based "slow download" page.
        """
        return self._get(
            f"/games/{self.game_domain}/mods/{mod_id}/files/{file_id}/download_link.json"
        )

    def get_main_file(self, mod_id: int) -> Optional[dict]:
        """Return the primary/main file for a mod, or None.

        Looks for category_id == 1 (main file), preferring the most
        recently uploaded if there are several.
        """
        files = self.get_mod_files(mod_id)
        main_files = [f for f in files if f.get("category_id") == 1]
        if main_files:
            # Sort by file_id descending (highest = newest upload)
            main_files.sort(key=lambda f: f.get("file_id", 0), reverse=True)
            return main_files[0]
        # Fallback: newest file of any category
        if files:
            files.sort(key=lambda f: f.get("file_id", 0), reverse=True)
            return files[0]
        return None

    def get_mod_updated(self, mod_id: int) -> Optional[datetime]:
        """Return the last-updated datetime for *mod_id* (UTC)."""
        info = self.get_mod_info(mod_id)
        ts = info.get("updated_timestamp")
        if ts:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        return None

    def get_mod_details(self, mod_id: int) -> dict:
        """
        Convenience method returning a summary dict with the fields
        the GUI cares about.
        """
        info = self.get_mod_info(mod_id)
        updated_ts = info.get("updated_timestamp") or info.get("created_timestamp", 0)
        return {
            "nexus_mod_id": mod_id,
            "name": info.get("name", f"Mod {mod_id}"),
            "version": info.get("version", "?"),
            "author": info.get("author", "Unknown"),
            "summary": info.get("summary", ""),
            "nexus_updated": datetime.fromtimestamp(updated_ts, tz=timezone.utc),
            "nexus_url": f"{NEXUS_WEB_BASE}/{self.game_domain}/mods/{mod_id}",
        }


# ------------------------------------------------------------------
# Web-scraping fallback (used when no API key is provided)
# ------------------------------------------------------------------


def scrape_mod_updated(mod_id: int, game_domain: str = "baldursgate3") -> Optional[dict]:
    """
    Scrape the Nexus Mods web page for *mod_id* and return basic info.

    This is a best-effort fallback when no API key is available.
    Nexus may block or rate-limit scraping, so this is less reliable.
    """
    url = f"{NEXUS_WEB_BASE}/{game_domain}/mods/{mod_id}"
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        if resp.status_code != 200:
            return None
    except requests.RequestException:
        return None

    html = resp.text

    # Try to extract mod name from <title>
    name_match = re.search(r"<title>(.+?)(?:\s+at\s+|\s*\|)", html)
    name = name_match.group(1).strip() if name_match else f"Mod {mod_id}"

    # Try to find the "Last updated" date – Nexus uses a <time> tag
    time_match = re.search(
        r'<time[^>]*datetime="([^"]+)"[^>]*>', html
    )
    updated = None
    if time_match:
        try:
            updated = datetime.fromisoformat(time_match.group(1).replace("Z", "+00:00"))
        except ValueError:
            pass

    return {
        "nexus_mod_id": mod_id,
        "name": name,
        "version": "?",
        "author": "Unknown",
        "summary": "",
        "nexus_updated": updated,
        "nexus_url": url,
    }
