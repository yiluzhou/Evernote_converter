"""Tests for OneNote uploader duplicate-detection helpers."""

import os
import sys
import types
from datetime import datetime

import pytest
import requests

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Provide a lightweight msal stub so uploader module imports in test envs
# where authentication deps are not installed.
if "msal" not in sys.modules:
    sys.modules["msal"] = types.SimpleNamespace(
        SerializableTokenCache=object,
        PublicClientApplication=object,
    )

from enex_parser import EvernoteNote, EvernoteResource
from onenote_uploader import (
    AzureSetupGuidanceError,
    GRAPH_BASE,
    OneNoteUploader,
    OneNoteRequestSizeLimitError,
    _classify_azure_setup_error,
    analyze_note_multipart_limits,
    get_graph_token,
    is_probable_request_size_error,
)


class _FakeResponse:
    def __init__(
        self,
        payload: dict | None = None,
        text: str = "",
        status_code: int = 200,
        headers: dict | None = None,
    ):
        self._payload = payload
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}

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


def test_delete_page_treats_404_as_already_deleted(monkeypatch):
    uploader = OneNoteUploader("token")
    uploader._page_guid_cache["page-404"] = "guid"
    uploader._page_url_cache["page-404"] = "https://example.test/page"

    def _raise_404(*args, **kwargs):
        resp = requests.Response()
        resp.status_code = 404
        resp.url = "https://graph.microsoft.com/v1.0/me/onenote/pages/page-404"
        raise requests.HTTPError("404", response=resp)

    monkeypatch.setattr(uploader, "_request_no_content_with_retry", _raise_404)

    uploader.delete_page("page-404")

    assert "page-404" not in uploader._page_guid_cache
    assert "page-404" not in uploader._page_url_cache


def test_delete_page_re_raises_non_404_http_error(monkeypatch):
    uploader = OneNoteUploader("token")

    def _raise_500(*args, **kwargs):
        resp = requests.Response()
        resp.status_code = 500
        resp.url = "https://graph.microsoft.com/v1.0/me/onenote/pages/page-500"
        raise requests.HTTPError("500", response=resp)

    monkeypatch.setattr(uploader, "_request_no_content_with_retry", _raise_500)

    with pytest.raises(requests.HTTPError):
        uploader.delete_page("page-500")


def test_request_with_retry_refreshes_token_on_401(monkeypatch):
    class _Session401ThenOK:
        def __init__(self):
            self.calls = 0
            self.headers = {"Authorization": "Bearer old-token"}

        def request(self, method: str, url: str, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _FakeResponse(
                    payload={
                        "error": {
                            "code": "InvalidAuthenticationToken",
                            "message": "Access token has expired.",
                        }
                    },
                    status_code=401,
                    text='{"error":{"code":"InvalidAuthenticationToken"}}',
                )
            return _FakeResponse(payload={"id": "page-123"}, status_code=201, text='{"id":"page-123"}')

    uploader = OneNoteUploader(
        "old-token",
        token_refresh_callback=lambda: "new-token",
    )
    uploader.session = _Session401ThenOK()
    monkeypatch.setattr(uploader, "_apply_client_side_write_rate_limit", lambda: None)

    payload = uploader._request_with_retry("POST", "https://example.test/pages")

    assert payload["id"] == "page-123"
    assert uploader.session.calls == 2
    assert uploader.session.headers["Authorization"] == "Bearer new-token"


def test_request_with_retry_retries_on_504_then_succeeds(monkeypatch):
    class _Session504ThenOK:
        def __init__(self):
            self.calls = 0
            self.headers = {"Authorization": "Bearer token"}

        def request(self, method: str, url: str, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _FakeResponse(
                    payload={
                        "error": {
                            "code": "GatewayTimeout",
                            "message": "The upstream service timed out.",
                        }
                    },
                    status_code=504,
                    text='{"error":{"code":"GatewayTimeout"}}',
                )
            return _FakeResponse(payload={"id": "page-504"}, status_code=201, text='{"id":"page-504"}')

    uploader = OneNoteUploader("token")
    uploader.session = _Session504ThenOK()
    monkeypatch.setattr(uploader, "_apply_client_side_write_rate_limit", lambda: None)
    sleeps: list[float] = []
    monkeypatch.setattr(sys.modules["onenote_uploader"].time, "sleep", lambda s: sleeps.append(s))

    payload = uploader._request_with_retry("POST", "https://example.test/pages")

    assert payload["id"] == "page-504"
    assert uploader.session.calls == 2
    assert sleeps


def test_request_no_content_with_retry_retries_on_504_then_succeeds(monkeypatch):
    class _Session504Then204:
        def __init__(self):
            self.calls = 0
            self.headers = {"Authorization": "Bearer token"}

        def request(self, method: str, url: str, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _FakeResponse(
                    payload={
                        "error": {
                            "code": "GatewayTimeout",
                            "message": "The upstream service timed out.",
                        }
                    },
                    status_code=504,
                    text='{"error":{"code":"GatewayTimeout"}}',
                )
            return _FakeResponse(payload={}, status_code=204, text="")

    uploader = OneNoteUploader("token")
    uploader.session = _Session504Then204()
    monkeypatch.setattr(uploader, "_apply_client_side_write_rate_limit", lambda: None)
    sleeps: list[float] = []
    monkeypatch.setattr(sys.modules["onenote_uploader"].time, "sleep", lambda s: sleeps.append(s))

    uploader._request_no_content_with_retry("DELETE", "https://example.test/pages/page-1")

    assert uploader.session.calls == 2
    assert sleeps


def test_get_graph_token_device_flow_callback_receives_flow(monkeypatch, tmp_path):
    class _FakeCache:
        def deserialize(self, _text):
            return None

        def serialize(self):
            return "{}"

    class _FakeApp:
        def __init__(self, client_id, authority, token_cache):
            self.client_id = client_id
            self.authority = authority
            self.token_cache = token_cache

        def get_accounts(self):
            return []

        def initiate_device_flow(self, scopes):
            return {
                "user_code": "ABCD-1234",
                "verification_uri": "https://microsoft.com/devicelogin",
                "message": "Use the code",
            }

        def acquire_token_by_device_flow(self, flow):
            assert flow["user_code"] == "ABCD-1234"
            return {"access_token": "token-123"}

    monkeypatch.setattr(
        sys.modules["onenote_uploader"].msal,
        "SerializableTokenCache",
        _FakeCache,
    )
    monkeypatch.setattr(
        sys.modules["onenote_uploader"].msal,
        "PublicClientApplication",
        _FakeApp,
    )
    monkeypatch.setattr(sys.modules["onenote_uploader"], "CACHE_FILE", str(tmp_path / "cache.json"))

    seen = {}

    def _callback(flow):
        seen.update(flow)

    token = sys.modules["onenote_uploader"].get_graph_token(
        "client-id",
        device_flow_callback=_callback,
    )

    assert token == "token-123"
    assert seen.get("user_code") == "ABCD-1234"


def test_get_graph_token_silent_only_raises_when_cache_has_no_valid_session(monkeypatch, tmp_path):
    class _FakeCache:
        def deserialize(self, _text):
            return None

        def serialize(self):
            return "{}"

    class _FakeApp:
        def __init__(self, client_id, authority, token_cache):
            self.client_id = client_id
            self.authority = authority
            self.token_cache = token_cache

        def get_accounts(self):
            return []

        def initiate_device_flow(self, scopes):  # pragma: no cover
            raise AssertionError("silent_only should not start device flow")

    monkeypatch.setattr(
        sys.modules["onenote_uploader"].msal,
        "SerializableTokenCache",
        _FakeCache,
    )
    monkeypatch.setattr(
        sys.modules["onenote_uploader"].msal,
        "PublicClientApplication",
        _FakeApp,
    )
    monkeypatch.setattr(sys.modules["onenote_uploader"], "CACHE_FILE", str(tmp_path / "cache.json"))

    with pytest.raises(AzureSetupGuidanceError) as exc:
        get_graph_token("client-id", silent_only=True)

    assert "expired" in str(exc.value.title).lower() or "authorization" in str(exc.value.title).lower()


def test_analyze_note_multipart_limits_reports_hard_violations(monkeypatch):
    module = sys.modules["onenote_uploader"]
    monkeypatch.setattr(module, "ONENOTE_MULTIPART_MAX_PART_BYTES", 80)
    monkeypatch.setattr(module, "ONENOTE_MULTIPART_MAX_TOTAL_BYTES", 120)
    monkeypatch.setattr(module, "ONENOTE_MULTIPART_MAX_PARTS", 2)
    monkeypatch.setattr(module, "ONENOTE_MULTIPART_WARNING_RATIO", 0.9)

    note = EvernoteNote(
        title="Big multipart",
        created=datetime(2025, 1, 1, 0, 0, 0),
        updated=datetime(2025, 1, 1, 0, 0, 0),
        content_enml="",
        resources=[
            EvernoteResource(
                data=b"A" * 90,
                mime="application/octet-stream",
                filename="a.bin",
                md5_hash="hash-a",
            ),
            EvernoteResource(
                data=b"B" * 40,
                mime="application/octet-stream",
                filename="b.bin",
                md5_hash="hash-b",
            ),
        ],
    )

    analysis = analyze_note_multipart_limits(
        note,
        '<img src="name:hash-a"/><object data="name:hash-b"></object>',
    )

    assert analysis["uses_multipart"] is True
    assert analysis["multipart_part_count"] == 3
    assert analysis["violations"]
    assert any("part count" in message for message in analysis["violations"])


def test_create_page_fails_locally_when_request_size_limit_exceeded(monkeypatch):
    module = sys.modules["onenote_uploader"]
    monkeypatch.setattr(module, "ONENOTE_MULTIPART_MAX_PART_BYTES", 50)
    monkeypatch.setattr(module, "ONENOTE_MULTIPART_MAX_TOTAL_BYTES", 60)
    monkeypatch.setattr(module, "ONENOTE_MULTIPART_MAX_PARTS", 500)

    note = EvernoteNote(
        title="Will fail size check",
        created=datetime(2025, 1, 1, 0, 0, 0),
        updated=datetime(2025, 1, 1, 0, 0, 0),
        content_enml="",
        resources=[
            EvernoteResource(
                data=b"C" * 55,
                mime="application/octet-stream",
                filename="c.bin",
                md5_hash="hash-c",
            )
        ],
    )
    uploader = OneNoteUploader("token")

    with pytest.raises(OneNoteRequestSizeLimitError):
        uploader.create_page(
            "section-1",
            note,
            '<object data="name:hash-c"></object>',
        )


def test_client_side_write_rate_limiter_waits_when_minute_window_is_full(monkeypatch):
    module = sys.modules["onenote_uploader"]
    uploader = OneNoteUploader("token")
    uploader._write_limit_per_minute = 1
    uploader._write_limit_per_hour = 0

    class _Clock:
        now = 1000.0

    clock = _Clock()
    sleeps: list[float] = []

    def _fake_monotonic():
        return clock.now

    def _fake_sleep(seconds: float):
        sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(module.time, "monotonic", _fake_monotonic)
    monkeypatch.setattr(module.time, "sleep", _fake_sleep)

    uploader._apply_client_side_write_rate_limit()
    uploader._apply_client_side_write_rate_limit()

    assert sleeps
    assert sleeps[0] >= 59.9


def test_estimate_upload_duration_seconds_uses_hour_limit_when_slower():
    uploader = OneNoteUploader("token")
    uploader._write_limit_per_minute = 120
    uploader._write_limit_per_hour = 60

    estimate = uploader.estimate_upload_duration_seconds(
        expected_page_writes=10,
        expected_section_creates=2,
    )

    # 12 writes at 60/hour -> at least 12 minutes (+small overhead)
    assert estimate >= (12 * 60)


def test_is_probable_request_size_error_recognizes_http_413():
    response = _FakeResponse(
        payload={"error": {"code": "RequestEntityTooLarge", "message": "Payload too large"}},
        status_code=413,
    )
    try:
        response.raise_for_status()
    except RuntimeError:
        pass
    # Build a requests.HTTPError with attached response.
    import requests

    exc = requests.HTTPError("413", response=response)
    assert is_probable_request_size_error(exc) is True
