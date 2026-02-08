"""Release update checks for GUI startup."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import requests

logger = logging.getLogger(__name__)

APP_VERSION = "1.0.0"
DEFAULT_RELEASE_REPO = "yiluzhou/Evernote_converter"
_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")


def parse_version_tuple(version_text: str) -> tuple[int, ...] | None:
    """
    Parse semver-ish tags like "v1.2.3" or "release-1.2.3-beta" to integer tuples.
    """
    if not version_text:
        return None

    match = _VERSION_RE.search(version_text.strip())
    if not match:
        return None

    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return None


def is_newer_version(current_version: str, latest_version: str) -> bool:
    """Return True when latest_version is newer than current_version."""
    current = parse_version_tuple(current_version)
    latest = parse_version_tuple(latest_version)
    if current is None or latest is None:
        return False

    width = max(len(current), len(latest))
    current_padded = current + (0,) * (width - len(current))
    latest_padded = latest + (0,) * (width - len(latest))
    return latest_padded > current_padded


def select_download_url(assets: list[dict[str, Any]], fallback_url: str) -> str:
    """
    Pick a user-facing download URL from release assets.

    Preference order:
    1) EvernoteToOneNoteWizard.exe (or any .exe containing "wizard")
    2) Any ZIP
    3) Fallback release URL
    """
    if not assets:
        return fallback_url

    normalized_assets: list[dict[str, str]] = []
    for asset in assets:
        name = str(asset.get("name", "")).strip()
        url = str(asset.get("browser_download_url", "")).strip()
        if not name or not url:
            continue
        normalized_assets.append({"name": name, "url": url})

    if not normalized_assets:
        return fallback_url

    preferred_exes = [
        a
        for a in normalized_assets
        if a["name"].lower().endswith(".exe")
        and "wizard" in a["name"].lower()
    ]
    if preferred_exes:
        return preferred_exes[0]["url"]

    any_exes = [a for a in normalized_assets if a["name"].lower().endswith(".exe")]
    if any_exes:
        return any_exes[0]["url"]

    any_zips = [a for a in normalized_assets if a["name"].lower().endswith(".zip")]
    if any_zips:
        return any_zips[0]["url"]

    return fallback_url


def get_latest_release_update(
    current_version: str = APP_VERSION,
    api_url: str | None = None,
    fallback_page_url: str | None = None,
    timeout_sec: float = 3.5,
) -> dict[str, str] | None:
    """
    Fetch latest GitHub release info and return update data if newer.

    Returns:
      {
        "latest_version": "<tag>",
        "download_url": "<asset or release page>",
        "release_url": "<release page>"
      }
    """
    repo = os.environ.get("ENEX_RELEASES_REPO", DEFAULT_RELEASE_REPO).strip()
    resolved_api_url = api_url or os.environ.get(
        "ENEX_RELEASES_API_URL",
        f"https://api.github.com/repos/{repo}/releases/latest",
    )
    resolved_fallback_page_url = fallback_page_url or os.environ.get(
        "ENEX_RELEASES_PAGE_URL",
        f"https://github.com/{repo}/releases/latest",
    )

    try:
        response = requests.get(
            resolved_api_url,
            headers={"Accept": "application/vnd.github+json"},
            timeout=timeout_sec,
        )
    except Exception as e:
        logger.debug("Update check request failed: %s", e)
        return None

    if response.status_code != 200:
        logger.debug("Update check returned HTTP %s", response.status_code)
        return None

    try:
        payload = response.json()
    except Exception as e:
        logger.debug("Update check JSON parse failed: %s", e)
        return None

    latest_version = str(payload.get("tag_name", "")).strip()
    if not latest_version:
        return None

    if not is_newer_version(current_version, latest_version):
        return None

    release_url = str(payload.get("html_url", "")).strip() or resolved_fallback_page_url
    assets = payload.get("assets")
    if not isinstance(assets, list):
        assets = []
    download_url = select_download_url(assets, release_url)

    return {
        "latest_version": latest_version,
        "download_url": download_url,
        "release_url": release_url,
    }
