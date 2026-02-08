"""Tests for OneNote uploader duplicate-detection helpers."""

import os
import sys
import types
from datetime import datetime

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Provide a lightweight msal stub so uploader module imports in test envs
# where authentication deps are not installed.
if "msal" not in sys.modules:
    sys.modules["msal"] = types.SimpleNamespace(
        SerializableTokenCache=object,
        PublicClientApplication=object,
    )

from enex_parser import EvernoteNote
from onenote_uploader import (
    AzureSetupGuidanceError,
    GRAPH_BASE,
    OneNoteUploader,
    _classify_azure_setup_error,
)


class _FakeResponse:
    def __init__(self, payload: dict | None = None, text: str = "", status_code: int = 200):
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        if self._payload is None:
            raise RuntimeError("No JSON payload")
        return self._payload


class _FakeSession:
    def __init__(self, get_payloads_by_url: dict[str, _FakeResponse], delete_status: int = 204):
        self._get_payloads_by_url = get_payloads_by_url
        self._delete_status = delete_status
        self.get_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str) -> _FakeResponse:
        self.get_calls.append(url)
        if url not in self._get_payloads_by_url:
            raise RuntimeError(f"Unexpected URL: {url}")
        return self._get_payloads_by_url[url]

    def delete(self, url: str) -> _FakeResponse:
        self.delete_calls.append(url)
        return _FakeResponse(payload={}, status_code=self._delete_status)


def test_page_identity_from_note_uses_normalized_title_and_utc_second():
    note = EvernoteNote(
        title="  My Note  ",
        created=datetime(2025, 1, 2, 3, 4, 5, 999000),
        updated=datetime(2025, 1, 2, 3, 4, 5),
        content_enml="<en-note><div>hello</div></en-note>",
    )

    identity = OneNoteUploader.page_identity_from_note(note)

    assert identity == ("My Note", "2025-01-02T03:04:05Z")


def test_page_identity_from_graph_page_parses_z_and_offset():
    identity_z = OneNoteUploader.page_identity_from_graph_page(
        {"title": "  Test  ", "createdDateTime": "2025-01-01T12:00:00.250Z"}
    )
    identity_offset = OneNoteUploader.page_identity_from_graph_page(
        {"title": "Test", "createdDateTime": "2025-01-01T13:30:00+01:30"}
    )

    assert identity_z == ("Test", "2025-01-01T12:00:00Z")
    assert identity_offset == ("Test", "2025-01-01T12:00:00Z")
    assert OneNoteUploader.page_identity_from_graph_page(
        {"title": "Broken", "createdDateTime": "not-a-date"}
    ) is None


def test_get_page_evernote_guid_reads_html_meta_and_uses_cache():
    page_id = "page-1"
    content_url = f"{GRAPH_BASE}/me/onenote/pages/{page_id}/content"

    uploader = OneNoteUploader("token")
    uploader.session = _FakeSession(
        {
            content_url: _FakeResponse(
                text='''<!DOCTYPE html><html><head><meta name="evernote-guid" content="guid-abc" /></head></html>'''
            )
        }
    )

    first = uploader.get_page_evernote_guid(page_id)
    second = uploader.get_page_evernote_guid(page_id)

    assert first == "guid-abc"
    assert second == "guid-abc"
    assert uploader.session.get_calls == [content_url]


def test_build_section_duplicate_index_uses_guid_and_fallback_identity():
    section_id = "section-123"
    first_url = f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages?$top=100"

    page_guid = "page-guid"
    page_fallback = "page-fallback"
    page_other = "page-other"

    uploader = OneNoteUploader("token")
    uploader.session = _FakeSession(
        {
            first_url: _FakeResponse(
                payload={
                    "value": [
                        {
                            "id": page_guid,
                            "title": "Matches Guid",
                            "createdDateTime": "2025-01-01T00:00:00Z",
                            "lastModifiedDateTime": "2025-01-02T00:00:00Z",
                            "links": {"oneNoteWebUrl": {"href": "https://example.com/p-guid"}},
                        },
                        {
                            "id": page_fallback,
                            "title": "Fallback Note",
                            "createdDateTime": "2025-01-10T09:30:00Z",
                            "lastModifiedDateTime": "2025-01-10T10:30:00Z",
                            "links": {"oneNoteWebUrl": {"href": "https://example.com/p-fallback"}},
                        },
                        {
                            "id": page_other,
                            "title": "Other",
                            "createdDateTime": "2025-01-03T00:00:00Z",
                        },
                    ]
                }
            ),
            f"{GRAPH_BASE}/me/onenote/pages/{page_guid}/content": _FakeResponse(
                text='<meta name="evernote-guid" content="guid-1" />'
            ),
            f"{GRAPH_BASE}/me/onenote/pages/{page_fallback}/content": _FakeResponse(
                text='<html><head></head><body>No guid</body></html>'
            ),
            f"{GRAPH_BASE}/me/onenote/pages/{page_other}/content": _FakeResponse(
                text='<meta name="evernote-guid" content="guid-other" />'
            ),
        }
    )

    notes = [
        EvernoteNote(
            title="Incoming Guid",
            guid="guid-1",
            created=datetime(2025, 1, 8, 12, 0, 0),
            updated=datetime(2025, 1, 8, 12, 0, 0),
            content_enml="",
        ),
        EvernoteNote(
            title="Fallback Note",
            created=datetime(2025, 1, 10, 9, 30, 0),
            updated=datetime(2025, 1, 10, 9, 30, 0),
            content_enml="",
        ),
    ]

    by_guid, by_fallback, total = uploader.build_section_duplicate_index(section_id, notes)

    assert total == 3
    assert "guid-1" in by_guid
    assert by_guid["guid-1"][0]["id"] == page_guid

    fallback_key = OneNoteUploader.page_identity_from_note(notes[1])
    assert fallback_key in by_fallback
    assert by_fallback[fallback_key][0]["id"] == page_fallback


def test_classify_azure_error_invalid_client_id():
    err = _classify_azure_setup_error(
        "AADSTS700016: Application with identifier 'bad-id' was not found in the directory."
    )

    assert isinstance(err, AzureSetupGuidanceError)
    assert "client ID" in err.title
    assert "Application (client) ID" in err.details
    assert "Check these items in order" in err.chatgpt_prompt


def test_raise_for_status_with_guidance_for_permission_failure():
    uploader = OneNoteUploader("token")
    resp = _FakeResponse(
        payload={
            "error": {
                "code": "AccessDenied",
                "message": "Insufficient privileges to complete the operation.",
            }
        },
        status_code=403,
    )

    try:
        uploader._raise_for_status_with_guidance(resp, "Create page")
        assert False, "Expected AzureSetupGuidanceError for permission failure"
    except AzureSetupGuidanceError as e:
        assert "permissions" in e.details.lower()


def test_raise_for_status_with_guidance_falls_back_to_http_error():
    uploader = OneNoteUploader("token")
    resp = _FakeResponse(
        payload={"error": {"code": "InternalServerError", "message": "boom"}},
        status_code=500,
    )

    try:
        uploader._raise_for_status_with_guidance(resp, "List pages")
        assert False, "Expected HTTP error passthrough"
    except RuntimeError as e:
        assert "HTTP 500" in str(e)
