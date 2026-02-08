"""
OneNote Uploader — Phase 2 of Evernote-to-OneNote Converter

Authenticates with Microsoft Graph API via MSAL device code flow and
uploads notes as OneNote pages with inline images and file attachments.

Prerequisites:
  1. Register an app in Azure Portal (see Agents.md section 4)
  2. Set ENEX_CLIENT_ID env var or pass --client-id to CLI

Reference:
  - MS Graph OneNote API: https://learn.microsoft.com/en-us/graph/onenote-create-page
  - MSAL Python: https://learn.microsoft.com/en-us/entra/msal/python/
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone

import msal
import requests

from enex_parser import EvernoteNote, build_onenote_page_html

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Notes.Create", "Notes.ReadWrite"]
AUTHORITY = "https://login.microsoftonline.com/consumers"  # personal MS accounts
CACHE_FILE = os.path.expanduser("~/.evernote_converter/token_cache.json")
EVERNOTE_GUID_META_RE = re.compile(
    r"""<meta[^>]*name=["']evernote-guid["'][^>]*content=["']([^"']+)["'][^>]*>""",
    re.IGNORECASE,
)


class AzureSetupGuidanceError(RuntimeError):
    """User-facing Azure setup error with actionable remediation guidance."""

    def __init__(self, title: str, details: str, chatgpt_prompt: str):
        super().__init__(details)
        self.title = title
        self.details = details
        self.chatgpt_prompt = chatgpt_prompt

    def to_user_message(self) -> str:
        return (
            f"{self.title}\n\n"
            f"{self.details}\n\n"
            "If still stuck, take a screenshot of your Azure page and ask ChatGPT with this prompt:\n"
            f"{self.chatgpt_prompt}"
        )


def _build_chatgpt_prompt(error_text: str) -> str:
    compact_error = " ".join((error_text or "").split())
    return (
        "I am configuring an Azure app for an Evernote-to-OneNote converter and got this error:\n"
        f"\"{compact_error}\"\n\n"
        "Please give me exact click-by-click steps in Azure Portal to fix it. "
        "Check these items in order:\n"
        "1) Application (client) ID is correct\n"
        "2) Supported account type is Personal Microsoft accounts only\n"
        "3) API delegated permissions include Notes.Create and Notes.ReadWrite\n"
        "4) Authentication -> Allow public client flows is set to Yes\n"
        "5) Any consent/sign-in step I missed"
    )


def _classify_azure_setup_error(error_text: str) -> AzureSetupGuidanceError:
    text = (error_text or "").strip()
    lower = text.lower()
    prompt = _build_chatgpt_prompt(text or "Unknown Azure authentication/permission error")

    if (
        "aadsts700016" in lower
        or "application with identifier" in lower
        or "was not found in the directory" in lower
        or "invalid client" in lower
    ):
        return AzureSetupGuidanceError(
            "Azure app client ID is invalid",
            (
                "The Application (client) ID appears incorrect for this account/tenant.\n"
                "Re-open Azure Portal -> App registrations -> your app -> Overview, then copy the exact client ID."
            ),
            prompt,
        )

    if (
        "aadsts7000218" in lower
        or "client_secret" in lower
        or "client assertion" in lower
        or "public client" in lower
        or "allow public client flows" in lower
    ):
        return AzureSetupGuidanceError(
            "Public client flow is not enabled",
            (
                "Your app is behaving like a confidential client. Enable device-code/public client flow:\n"
                "Azure Portal -> App registrations -> your app -> Authentication -> "
                "Allow public client flows = Yes -> Save."
            ),
            prompt,
        )

    if (
        "notes.create" in lower
        or "notes.readwrite" in lower
        or "accessdenied" in lower
        or "insufficient privileges" in lower
        or "authorization_requestdenied" in lower
        or "aadsts65001" in lower
        or "consent" in lower
        or "scope" in lower
    ):
        return AzureSetupGuidanceError(
            "Microsoft Graph permissions are missing or not consented",
            (
                "Your app likely lacks required delegated permissions.\n"
                "In Azure Portal -> API permissions, add Microsoft Graph delegated permissions:\n"
                "- Notes.Create\n"
                "- Notes.ReadWrite\n"
                "Then retry sign-in and consent if prompted."
            ),
            prompt,
        )

    if "aadsts50020" in lower or "personal microsoft accounts" in lower:
        return AzureSetupGuidanceError(
            "Account type/tenant mismatch",
            (
                "Your app registration or sign-in account does not match the expected personal-account flow.\n"
                "Set Supported account types to Personal Microsoft accounts only and sign in with that account."
            ),
            prompt,
        )

    return AzureSetupGuidanceError(
        "Azure setup issue detected",
        (
            "Authentication or Graph access failed due to Azure app configuration.\n"
            "Verify client ID, Notes.Create/Notes.ReadWrite delegated permissions, and "
            "Allow public client flows = Yes."
        ),
        prompt,
    )


def _extract_graph_error_text(resp: requests.Response) -> str:
    try:
        payload = resp.json()
    except Exception:
        payload = {}

    error_obj = payload.get("error", {}) if isinstance(payload, dict) else {}
    code = str(error_obj.get("code", "")).strip()
    message = str(error_obj.get("message", "")).strip()
    parts = [p for p in [code, message] if p]
    if parts:
        return " - ".join(parts)

    body = (resp.text or "").strip()
    if body:
        return body
    return f"HTTP {resp.status_code}"


def _normalize_title(title: str | None) -> str:
    return (title or "").strip()


def _to_utc_second_key(dt: datetime) -> str:
    """
    Convert a datetime into a stable UTC key with second precision.

    Graph may return varying timezone/millisecond formats; this canonical form
    lets us compare page identities safely.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_graph_datetime(value: str | None) -> datetime | None:
    """Parse Graph datetime strings like 2025-01-01T12:00:00Z."""
    if not value:
        return None

    candidate = value.strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"

    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        logger.debug("Unable to parse Graph datetime: %s", value)
        return None


def _extract_web_url(item: dict) -> str:
    return (
        item.get("links", {})
        .get("oneNoteWebUrl", {})
        .get("href", "")
    )


def _extract_evernote_guid_from_html(html: str) -> str:
    if not html:
        return ""
    match = EVERNOTE_GUID_META_RE.search(html)
    if not match:
        return ""
    return match.group(1).strip()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def get_graph_token(client_id: str) -> str:
    """
    Authenticate via device code flow and return an access token.

    On first run, prints a URL + code for the user to sign in via browser.
    Subsequent runs use cached tokens (auto-refresh via MSAL).
    """
    cache = msal.SerializableTokenCache()
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            cache.deserialize(f.read())

    try:
        app = msal.PublicClientApplication(
            client_id, authority=AUTHORITY, token_cache=cache
        )
    except Exception as e:
        raise _classify_azure_setup_error(str(e)) from e

    # Try silent acquisition first (cached/refreshed token)
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_cache(cache)
            return result["access_token"]

    # Fall back to device code flow
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        error_text = flow.get("error_description", "") or str(flow)
        raise _classify_azure_setup_error(f"Failed to create device flow: {error_text}")
    print(flow["message"])  # "To sign in, use a web browser to open..."
    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        error_text = result.get("error_description", "") or str(result)
        raise _classify_azure_setup_error(f"Authentication failed: {error_text}")

    _save_cache(cache)
    return result["access_token"]


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        f.write(cache.serialize())


# ---------------------------------------------------------------------------
# OneNote Graph API client
# ---------------------------------------------------------------------------

class OneNoteUploader:
    """Client for creating OneNote notebooks, sections, and pages via Graph API."""

    def __init__(self, access_token: str):
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {access_token}"
        self._page_guid_cache: dict[str, str] = {}
        self._page_url_cache: dict[str, str] = {}

    def get_or_create_notebook(self, name: str) -> tuple[str, bool]:
        """
        Get existing notebook by name, or create if doesn't exist.

        Returns:
            (notebook_id, was_created) tuple
        """
        notebooks = self.list_notebooks()

        # Check if notebook with this name exists
        for nb in notebooks:
            if nb.get("displayName") == name:
                logger.info("Found existing notebook: %s (id=%s)", name, nb["id"])
                return nb["id"], False

        # Create new notebook
        resp = self._post_json(
            f"{GRAPH_BASE}/me/onenote/notebooks",
            json={"displayName": name},
        )
        logger.info("Created new notebook: %s (id=%s)", name, resp["id"])
        return resp["id"], True

    def list_notebooks(self) -> list[dict]:
        """List all notebooks with IDs, display names, and web URLs."""
        notebooks: list[dict] = []
        url = f"{GRAPH_BASE}/me/onenote/notebooks?$top=100"
        while url:
            resp = self.session.get(url)
            self._raise_for_status_with_guidance(resp, "List notebooks")
            payload = resp.json()
            notebooks.extend(payload.get("value", []))
            url = payload.get("@odata.nextLink")

        notebooks.sort(key=lambda n: (n.get("displayName") or "").lower())
        return notebooks

    def create_notebook(self, name: str) -> str:
        """Create a notebook and return its ID (may create duplicate)."""
        resp = self._post_json(
            f"{GRAPH_BASE}/me/onenote/notebooks",
            json={"displayName": name},
        )
        logger.info("Created notebook: %s (id=%s)", name, resp["id"])
        return resp["id"]

    def get_notebook_web_url(self, notebook_id: str) -> str:
        """Get a notebook's OneNote web URL (if available)."""
        resp = self.session.get(f"{GRAPH_BASE}/me/onenote/notebooks/{notebook_id}")
        self._raise_for_status_with_guidance(resp, "Get notebook web URL")
        return _extract_web_url(resp.json())

    def get_or_create_section(self, notebook_id: str, name: str) -> tuple[str, bool]:
        """
        Get existing section by name in a notebook, or create if doesn't exist.

        Returns:
            (section_id, was_created) tuple
        """
        # List all sections in the notebook
        resp = self.session.get(
            f"{GRAPH_BASE}/me/onenote/notebooks/{notebook_id}/sections"
        )
        self._raise_for_status_with_guidance(resp, "List sections")
        sections = resp.json().get("value", [])

        # Check if section with this name exists
        for sec in sections:
            if sec.get("displayName") == name:
                logger.info("Found existing section: %s (id=%s)", name, sec["id"])
                return sec["id"], False

        # Create new section
        resp = self._post_json(
            f"{GRAPH_BASE}/me/onenote/notebooks/{notebook_id}/sections",
            json={"displayName": name},
        )
        logger.info("Created new section: %s (id=%s)", name, resp["id"])
        return resp["id"], True

    def create_section(self, notebook_id: str, name: str) -> str:
        """Create a section in a notebook and return its ID (may create duplicate)."""
        resp = self._post_json(
            f"{GRAPH_BASE}/me/onenote/notebooks/{notebook_id}/sections",
            json={"displayName": name},
        )
        logger.info("Created section: %s (id=%s)", name, resp["id"])
        return resp["id"]

    @staticmethod
    def page_identity_from_note(note: EvernoteNote) -> tuple[str, str]:
        """
        Build fallback identity for notes without GUID.

        Uses normalized title + created timestamp.
        """
        return (_normalize_title(note.title), _to_utc_second_key(note.created))

    @staticmethod
    def page_identity_from_graph_page(page: dict) -> tuple[str, str] | None:
        """Build the same fallback identity tuple from a Graph page object."""
        created = _parse_graph_datetime(page.get("createdDateTime"))
        if created is None:
            return None
        return (_normalize_title(page.get("title")), _to_utc_second_key(created))

    def list_section_pages(self, section_id: str) -> list[dict]:
        """
        List all pages in a section with pagination.

        Returns Graph page objects, including links for web URLs.
        """
        pages: list[dict] = []
        url = f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages?$top=100"

        while url:
            resp = self.session.get(url)
            self._raise_for_status_with_guidance(resp, "List section pages")
            payload = resp.json()
            pages.extend(payload.get("value", []))
            url = payload.get("@odata.nextLink")

        return pages

    def get_page_web_url(self, page_id: str) -> str:
        """Get a page's OneNote web URL."""
        cached = self._page_url_cache.get(page_id)
        if cached:
            return cached

        resp = self.session.get(f"{GRAPH_BASE}/me/onenote/pages/{page_id}")
        self._raise_for_status_with_guidance(resp, "Get page web URL")
        url = _extract_web_url(resp.json())
        if url:
            self._page_url_cache[page_id] = url
        return url

    def get_page_evernote_guid(self, page_id: str) -> str:
        """
        Extract embedded Evernote GUID from OneNote page HTML.

        GUID is stored in page `<meta name="evernote-guid" ...>` during upload.
        """
        if page_id in self._page_guid_cache:
            return self._page_guid_cache[page_id]

        resp = self.session.get(f"{GRAPH_BASE}/me/onenote/pages/{page_id}/content")
        self._raise_for_status_with_guidance(resp, "Get page content")
        guid = _extract_evernote_guid_from_html(resp.text)
        self._page_guid_cache[page_id] = guid
        return guid

    def delete_page(self, page_id: str) -> None:
        """Delete a page by ID."""
        resp = self.session.delete(f"{GRAPH_BASE}/me/onenote/pages/{page_id}")
        self._raise_for_status_with_guidance(resp, "Delete page")
        self._page_guid_cache.pop(page_id, None)
        self._page_url_cache.pop(page_id, None)
        logger.info("Deleted page: id=%s", page_id)

    def build_section_duplicate_index(
        self,
        section_id: str,
        notes: list[EvernoteNote],
    ) -> tuple[dict[str, list[dict]], dict[tuple[str, str], list[dict]], int]:
        """
        Build duplicate lookup indexes for a section.

        Returns:
            (by_guid, by_fallback_identity, total_pages_in_section)
        """
        by_guid: dict[str, list[dict]] = {}
        by_fallback: dict[tuple[str, str], list[dict]] = {}

        note_guids = {n.guid.strip() for n in notes if n.guid.strip()}
        fallback_keys = {self.page_identity_from_note(n) for n in notes}

        pages = self.list_section_pages(section_id)
        for page in pages:
            page_id = page.get("id", "")
            if not page_id:
                continue

            summary = {
                "id": page_id,
                "title": page.get("title", ""),
                "createdDateTime": page.get("createdDateTime", ""),
                "lastModifiedDateTime": page.get("lastModifiedDateTime", ""),
                "webUrl": _extract_web_url(page),
            }

            fallback_key = self.page_identity_from_graph_page(page)
            if fallback_key is not None and fallback_key in fallback_keys:
                summary["fallbackIdentity"] = fallback_key
                by_fallback.setdefault(fallback_key, []).append(summary)

            if note_guids:
                guid = self.get_page_evernote_guid(page_id)
                if guid and guid in note_guids:
                    summary["evernoteGuid"] = guid
                    by_guid.setdefault(guid, []).append(summary)

        return by_guid, by_fallback, len(pages)

    def create_page(
        self,
        section_id: str,
        note: EvernoteNote,
        html_body: str,
    ) -> str:
        """
        Create a OneNote page with content and optional attachments.

        Uses multipart/form-data when the note has resources referenced
        in the HTML body (via name:hash pattern).

        Returns the page ID.
        """
        page_html = build_onenote_page_html(note, html_body)
        url = f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages"

        # Find resources actually referenced in the HTML
        used_resources = [
            r for r in note.resources
            if f"name:{r.md5_hash}" in html_body
        ]

        if not used_resources:
            # Simple request — no attachments
            resp = self._post_html(url, page_html)
        else:
            # Multipart request with binary resource parts
            # The "Presentation" part contains the HTML page.
            # Each resource is a named part referenced via src="name:<partname>".
            files = {
                "Presentation": (
                    None,
                    page_html.encode("utf-8"),
                    "text/html; charset=utf-8",
                ),
            }
            for res in used_resources:
                files[res.md5_hash] = (res.filename, res.data, res.mime)

            resp = self._post_multipart(url, files)

        page_id = resp.get("id", "")
        logger.info("Created page: %s (id=%s)", note.title, page_id)
        return page_id

    # -- HTTP helpers with retry on 429 --

    def _post_json(self, url: str, json: dict) -> dict:
        """POST JSON with retry on rate limiting."""
        return self._request_with_retry("POST", url, json=json)

    def _post_html(self, url: str, html: str) -> dict:
        """POST raw HTML with retry on rate limiting."""
        return self._request_with_retry(
            "POST", url,
            data=html.encode("utf-8"),
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

    def _post_multipart(self, url: str, files: dict) -> dict:
        """POST multipart/form-data with retry on rate limiting."""
        return self._request_with_retry("POST", url, files=files)

    def _request_with_retry(
        self, method: str, url: str, max_retries: int = 5, **kwargs
    ) -> dict:
        """Execute an HTTP request with exponential backoff on 429."""
        for attempt in range(max_retries):
            resp = self.session.request(method, url, **kwargs)

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2**attempt))
                logger.warning("Rate limited, waiting %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
                continue

            if resp.status_code == 201:
                return resp.json()

            # Non-retryable error
            self._raise_for_status_with_guidance(resp, f"{method} {url}")

        raise RuntimeError(f"Request failed after {max_retries} retries: {url}")

    def _raise_for_status_with_guidance(self, resp: requests.Response, operation: str) -> None:
        if resp.status_code < 400:
            return

        error_text = _extract_graph_error_text(resp)
        lower = error_text.lower()

        if resp.status_code in {400, 401, 403}:
            if (
                "notes.create" in lower
                or "notes.readwrite" in lower
                or "accessdenied" in lower
                or "insufficient privileges" in lower
                or "authorization_requestdenied" in lower
                or "consent" in lower
                or "public client" in lower
                or "client_secret" in lower
                or "aadsts" in lower
                or "invalid client" in lower
            ):
                raise _classify_azure_setup_error(f"{operation} failed: {error_text}")

        resp.raise_for_status()
