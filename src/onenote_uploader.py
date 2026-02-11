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

from collections import deque
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import msal
import requests

from enex_parser import EvernoteNote, build_onenote_page_html

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Notes.Create", "Notes.ReadWrite"]
AUTHORITY = "https://login.microsoftonline.com/consumers"  # personal MS accounts
CACHE_FILE = os.path.expanduser("~/.evernote_converter/token_cache.json")
ONENOTE_MULTIPART_MAX_PART_BYTES = 25 * 1024 * 1024
ONENOTE_MULTIPART_MAX_TOTAL_BYTES = 75 * 1024 * 1024
ONENOTE_MULTIPART_MAX_PARTS = 500
# Estimated boundary/header overhead per multipart part for risk warnings.
ONENOTE_MULTIPART_PART_OVERHEAD_ESTIMATE_BYTES = 1024
ONENOTE_MULTIPART_WARNING_RATIO = 0.9
DEFAULT_WRITE_LIMIT_PER_MINUTE = 100
DEFAULT_WRITE_LIMIT_PER_HOUR = 350
_REQUEST_SIZE_ERROR_KEYWORDS = (
    "request entity too large",
    "payload too large",
    "request body too large",
    "request is too large",
    "maximum request length",
    "maximum request size",
    "exceeds the maximum",
    "too large",
)
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


class OneNoteRequestSizeLimitError(RuntimeError):
    """The note cannot be uploaded due to OneNote request-size limits."""


def _build_session_expired_error(error_text: str) -> AzureSetupGuidanceError:
    prompt = _build_chatgpt_prompt(error_text or "Session expired")
    return AzureSetupGuidanceError(
        "Sign-in expired or authorization lost",
        (
            "Your Microsoft Graph session is no longer authorized (token expired/revoked).\n"
            "Sign in again and resume upload.\n"
            "If this keeps happening, verify that device-code sign-in completed under the same Microsoft account."
        ),
        prompt,
    )


def _read_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", name, raw, default)
        return default
    return max(value, 0)


def _format_bytes(size: int) -> str:
    """Human-readable byte size."""
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    unit = units[0]
    for candidate in units:
        unit = candidate
        if value < 1024 or candidate == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} {unit}"
    return f"{value:.2f} {unit}"


def _looks_like_size_limit_error(status_code: int, error_text: str) -> bool:
    if status_code == 413:
        return True
    lowered = (error_text or "").lower()
    if not lowered:
        return False
    return any(keyword in lowered for keyword in _REQUEST_SIZE_ERROR_KEYWORDS)


def analyze_note_multipart_limits(
    note: EvernoteNote,
    html_body: str,
    page_html: str | None = None,
) -> dict[str, Any]:
    """
    Analyze OneNote multipart request-size limits for a note.

    Returns a dict with:
      - uses_multipart (bool)
      - used_resource_count (int)
      - used_resource_hashes (set[str])
      - multipart_part_count (int)
      - presentation_size_bytes (int)
      - largest_part_size_bytes (int)
      - total_payload_bytes (int)
      - total_payload_with_overhead_estimate_bytes (int)
      - violations (list[str]) hard limit breaches
      - warnings (list[str]) near-limit/risk warnings
    """
    if page_html is None:
        page_html = build_onenote_page_html(note, html_body)

    presentation_size = len(page_html.encode("utf-8"))
    used_resources = [r for r in note.resources if f"name:{r.md5_hash}" in html_body]
    used_hashes = {r.md5_hash for r in used_resources}
    uses_multipart = bool(used_resources)

    result: dict[str, Any] = {
        "uses_multipart": uses_multipart,
        "used_resource_count": len(used_resources),
        "used_resource_hashes": used_hashes,
        "multipart_part_count": 0,
        "presentation_size_bytes": presentation_size,
        "largest_part_size_bytes": 0,
        "total_payload_bytes": 0,
        "total_payload_with_overhead_estimate_bytes": 0,
        "violations": [],
        "warnings": [],
    }

    if not uses_multipart:
        return result

    part_sizes: list[tuple[str, int]] = [("Presentation", presentation_size)]
    part_sizes.extend((r.filename or r.md5_hash, len(r.data)) for r in used_resources)

    part_count = len(part_sizes)
    largest_part = max(size for _, size in part_sizes)
    total_payload = sum(size for _, size in part_sizes)
    total_with_overhead = (
        total_payload + part_count * ONENOTE_MULTIPART_PART_OVERHEAD_ESTIMATE_BYTES
    )

    violations: list[str] = []
    warnings: list[str] = []

    if part_count > ONENOTE_MULTIPART_MAX_PARTS:
        violations.append(
            f"multipart part count {part_count} exceeds "
            f"{ONENOTE_MULTIPART_MAX_PARTS}"
        )

    oversized_parts = [
        (name, size)
        for name, size in part_sizes
        if size > ONENOTE_MULTIPART_MAX_PART_BYTES
    ]
    if oversized_parts:
        examples = ", ".join(
            f"{name} ({_format_bytes(size)})"
            for name, size in oversized_parts[:3]
        )
        if len(oversized_parts) > 3:
            examples += ", ..."
        violations.append(
            "one or more multipart parts exceed "
            f"{_format_bytes(ONENOTE_MULTIPART_MAX_PART_BYTES)} "
            f"(examples: {examples})"
        )

    if total_payload > ONENOTE_MULTIPART_MAX_TOTAL_BYTES:
        violations.append(
            "multipart payload size "
            f"{_format_bytes(total_payload)} exceeds "
            f"{_format_bytes(ONENOTE_MULTIPART_MAX_TOTAL_BYTES)}"
        )

    if (
        total_payload <= ONENOTE_MULTIPART_MAX_TOTAL_BYTES
        and total_with_overhead > ONENOTE_MULTIPART_MAX_TOTAL_BYTES
    ):
        warnings.append(
            "multipart payload is near 75 MB; multipart boundary overhead may exceed the limit"
        )

    if (
        total_payload
        >= int(ONENOTE_MULTIPART_MAX_TOTAL_BYTES * ONENOTE_MULTIPART_WARNING_RATIO)
    ):
        warnings.append(
            "multipart payload is close to the 75 MB limit "
            f"({_format_bytes(total_payload)})"
        )

    if (
        largest_part
        >= int(ONENOTE_MULTIPART_MAX_PART_BYTES * ONENOTE_MULTIPART_WARNING_RATIO)
    ):
        warnings.append(
            "at least one multipart part is close to the 25 MB limit "
            f"({_format_bytes(largest_part)})"
        )

    if part_count >= int(ONENOTE_MULTIPART_MAX_PARTS * ONENOTE_MULTIPART_WARNING_RATIO):
        warnings.append(
            f"multipart part count is close to the {ONENOTE_MULTIPART_MAX_PARTS} limit "
            f"({part_count})"
        )

    result.update(
        {
            "multipart_part_count": part_count,
            "largest_part_size_bytes": largest_part,
            "total_payload_bytes": total_payload,
            "total_payload_with_overhead_estimate_bytes": total_with_overhead,
            "violations": violations,
            "warnings": warnings,
        }
    )
    return result


def is_probable_request_size_error(error: Exception) -> bool:
    """
    Best-effort classifier for request-size limit failures.

    Useful when service-side limits change and we need to skip oversized notes
    without aborting the whole import run.
    """
    if isinstance(error, OneNoteRequestSizeLimitError):
        return True

    if isinstance(error, requests.HTTPError):
        resp = getattr(error, "response", None)
        if resp is not None:
            try:
                error_text = _extract_graph_error_text(resp)
            except Exception:
                error_text = str(error)
            if _looks_like_size_limit_error(resp.status_code, error_text):
                return True

    return _looks_like_size_limit_error(0, str(error))


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
        "invalidauthenticationtoken" in lower
        or "token expired" in lower
        or "access token has expired" in lower
        or "lifetime validation failed" in lower
        or "expiredsecuritytoken" in lower
    ):
        return _build_session_expired_error(text)

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

def get_graph_token(
    client_id: str,
    device_flow_callback: Callable[[dict[str, Any]], None] | None = None,
    silent_only: bool = False,
) -> str:
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

    if silent_only:
        raise _build_session_expired_error(
            "Silent token refresh failed; cached session is unavailable."
        )

    # Fall back to device code flow
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        error_text = flow.get("error_description", "") or str(flow)
        raise _classify_azure_setup_error(f"Failed to create device flow: {error_text}")
    if device_flow_callback is not None:
        try:
            device_flow_callback(flow)
        except Exception as e:
            logger.debug("Device-flow callback failed: %s", e)
    else:
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

    def __init__(
        self,
        access_token: str,
        token_refresh_callback: Callable[[], str] | None = None,
    ):
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {access_token}"
        self._token_refresh_callback = token_refresh_callback
        self._token_refresh_lock = threading.Lock()
        self._page_guid_cache: dict[str, str] = {}
        self._page_url_cache: dict[str, str] = {}
        self._write_limit_per_minute = _read_int_env(
            "ENEX_GRAPH_WRITE_LIMIT_PER_MINUTE",
            DEFAULT_WRITE_LIMIT_PER_MINUTE,
        )
        self._write_limit_per_hour = _read_int_env(
            "ENEX_GRAPH_WRITE_LIMIT_PER_HOUR",
            DEFAULT_WRITE_LIMIT_PER_HOUR,
        )
        self._write_times_minute: deque[float] = deque()
        self._write_times_hour: deque[float] = deque()
        self._write_rate_lock = threading.Lock()

    def get_write_rate_limits(self) -> tuple[int, int]:
        """Return configured client-side write pacing limits (per-minute, per-hour)."""
        return self._write_limit_per_minute, self._write_limit_per_hour

    def estimate_upload_duration_seconds(
        self,
        expected_page_writes: int,
        expected_section_creates: int = 0,
        expected_page_deletes: int = 0,
        extra_write_requests: int = 0,
    ) -> float:
        """
        Estimate upload duration from expected write count and local rate limits.

        This is a lower-bound estimate under normal network conditions and
        without duplicate prompts/retries.
        """
        total_writes = max(expected_page_writes, 0)
        total_writes += max(expected_section_creates, 0)
        total_writes += max(expected_page_deletes, 0)
        total_writes += max(extra_write_requests, 0)

        if total_writes <= 0:
            return 0.0

        candidates: list[float] = []
        if self._write_limit_per_minute > 0:
            candidates.append((total_writes * 60.0) / self._write_limit_per_minute)
        if self._write_limit_per_hour > 0:
            candidates.append((total_writes * 3600.0) / self._write_limit_per_hour)

        if not candidates:
            return 0.0
        return max(candidates) + 5.0

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

    def list_sections(self, notebook_id: str) -> list[dict]:
        """List all sections in a notebook with IDs and display names."""
        sections: list[dict] = []
        url = f"{GRAPH_BASE}/me/onenote/notebooks/{notebook_id}/sections?$top=100"
        while url:
            resp = self.session.get(url)
            self._raise_for_status_with_guidance(resp, "List sections")
            payload = resp.json()
            sections.extend(payload.get("value", []))
            url = payload.get("@odata.nextLink")
        return sections

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
        try:
            self._request_no_content_with_retry(
                "DELETE",
                f"{GRAPH_BASE}/me/onenote/pages/{page_id}",
            )
        except requests.HTTPError as e:
            resp = getattr(e, "response", None)
            if resp is not None and resp.status_code == 404:
                # Treat as idempotent success: page was already removed.
                logger.info("Delete page skipped (already missing): id=%s", page_id)
                self._page_guid_cache.pop(page_id, None)
                self._page_url_cache.pop(page_id, None)
                return
            raise
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
        analysis = analyze_note_multipart_limits(note, html_body, page_html=page_html)
        if analysis["violations"]:
            raise OneNoteRequestSizeLimitError(
                f"Note '{note.title or 'Untitled'}' exceeds OneNote multipart limits: "
                + "; ".join(analysis["violations"])
            )
        for warning in analysis["warnings"]:
            logger.warning(
                "Note '%s': %s",
                note.title or "Untitled",
                warning,
            )

        url = f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages"

        # Find resources actually referenced in the HTML
        used_hashes: set[str] = analysis["used_resource_hashes"]
        used_resources = [
            r for r in note.resources
            if r.md5_hash in used_hashes
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
        refreshed_after_401 = False
        for attempt in range(max_retries):
            self._apply_client_side_write_rate_limit()
            resp = self.session.request(method, url, **kwargs)

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2**attempt))
                logger.warning("Rate limited, waiting %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
                continue

            if resp.status_code == 401 and not refreshed_after_401:
                if self._try_refresh_access_token():
                    refreshed_after_401 = True
                    logger.info("Refreshed access token after HTTP 401; retrying request.")
                    continue

            if 200 <= resp.status_code < 300:
                if not resp.text:
                    return {}
                try:
                    return resp.json()
                except ValueError:
                    return {}

            # Non-retryable error
            self._raise_for_status_with_guidance(resp, f"{method} {url}")

        raise RuntimeError(f"Request failed after {max_retries} retries: {url}")

    def _request_no_content_with_retry(
        self,
        method: str,
        url: str,
        max_retries: int = 5,
        **kwargs,
    ) -> None:
        """Execute an HTTP request expecting no JSON body, with 429 retry."""
        refreshed_after_401 = False
        for attempt in range(max_retries):
            self._apply_client_side_write_rate_limit()
            resp = self.session.request(method, url, **kwargs)

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2**attempt))
                logger.warning("Rate limited, waiting %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
                continue

            if resp.status_code == 401 and not refreshed_after_401:
                if self._try_refresh_access_token():
                    refreshed_after_401 = True
                    logger.info("Refreshed access token after HTTP 401; retrying request.")
                    continue

            if 200 <= resp.status_code < 300:
                return

            self._raise_for_status_with_guidance(resp, f"{method} {url}")

        raise RuntimeError(f"Request failed after {max_retries} retries: {url}")

    def _apply_client_side_write_rate_limit(self) -> None:
        """Proactively pace write requests to stay under delegated limits."""
        if self._write_limit_per_minute <= 0 and self._write_limit_per_hour <= 0:
            return

        while True:
            wait_seconds = 0.0
            with self._write_rate_lock:
                now = time.monotonic()

                if self._write_limit_per_minute > 0:
                    while self._write_times_minute and now - self._write_times_minute[0] >= 60:
                        self._write_times_minute.popleft()
                    if len(self._write_times_minute) >= self._write_limit_per_minute:
                        wait_seconds = max(
                            wait_seconds,
                            60 - (now - self._write_times_minute[0]),
                        )

                if self._write_limit_per_hour > 0:
                    while self._write_times_hour and now - self._write_times_hour[0] >= 3600:
                        self._write_times_hour.popleft()
                    if len(self._write_times_hour) >= self._write_limit_per_hour:
                        wait_seconds = max(
                            wait_seconds,
                            3600 - (now - self._write_times_hour[0]),
                        )

                if wait_seconds <= 0:
                    if self._write_limit_per_minute > 0:
                        self._write_times_minute.append(now)
                    if self._write_limit_per_hour > 0:
                        self._write_times_hour.append(now)
                    return

            wait_seconds = max(wait_seconds, 0.01)
            logger.info(
                "Client-side write throttling: sleeping %.2fs "
                "(limit %d/min, %d/hour)",
                wait_seconds,
                self._write_limit_per_minute,
                self._write_limit_per_hour,
            )
            time.sleep(wait_seconds)

    def _try_refresh_access_token(self) -> bool:
        """Attempt one silent token refresh via callback; return True on success."""
        if self._token_refresh_callback is None:
            return False

        with self._token_refresh_lock:
            try:
                new_token = self._token_refresh_callback()
            except AzureSetupGuidanceError:
                raise
            except Exception as e:
                raise _build_session_expired_error(str(e)) from e

            if not new_token:
                raise _build_session_expired_error("Token refresh callback returned empty token.")

            self.session.headers["Authorization"] = f"Bearer {new_token}"
            return True

    def _raise_for_status_with_guidance(self, resp: requests.Response, operation: str) -> None:
        if resp.status_code < 400:
            return

        error_text = _extract_graph_error_text(resp)
        lower = error_text.lower()

        if _looks_like_size_limit_error(resp.status_code, error_text):
            raise OneNoteRequestSizeLimitError(
                f"{operation} failed due to request-size limits: {error_text}"
            )

        if resp.status_code == 401:
            if (
                "invalidauthenticationtoken" in lower
                or "token expired" in lower
                or "access token has expired" in lower
                or "lifetime validation failed" in lower
                or "expiredsecuritytoken" in lower
                or "unauthorized" in lower
            ):
                raise _build_session_expired_error(f"{operation} failed: {error_text}")

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
