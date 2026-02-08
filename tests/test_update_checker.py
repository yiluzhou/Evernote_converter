"""Tests for release update-check helpers."""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import update_checker  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


def test_parse_version_tuple_supports_common_tag_formats():
    assert update_checker.parse_version_tuple("v1.2.3") == (1, 2, 3)
    assert update_checker.parse_version_tuple("release-2.0.1-beta") == (2, 0, 1)
    assert update_checker.parse_version_tuple("3") == (3,)
    assert update_checker.parse_version_tuple("alpha") is None


def test_is_newer_version_handles_different_widths():
    assert update_checker.is_newer_version("1.2", "1.2.1")
    assert update_checker.is_newer_version("1.2.0", "1.10.0")
    assert not update_checker.is_newer_version("1.2.1", "1.2.1")
    assert not update_checker.is_newer_version("1.2.1", "1.2.0")


def test_get_latest_release_update_returns_none_when_not_newer(monkeypatch):
    def _fake_get(*_args, **_kwargs):
        return _FakeResponse(
            {
                "tag_name": "v1.0.0",
                "html_url": "https://example.com/release",
                "assets": [],
            }
        )

    monkeypatch.setattr(update_checker.requests, "get", _fake_get)
    result = update_checker.get_latest_release_update(
        current_version="1.0.0",
        api_url="https://api.example.com/latest",
        fallback_page_url="https://example.com/releases/latest",
    )
    assert result is None


def test_get_latest_release_update_returns_repo_link(monkeypatch):
    def _fake_get(*_args, **_kwargs):
        return _FakeResponse(
            {
                "tag_name": "v1.3.0",
                "html_url": "https://github.com/org/repo/releases/tag/v1.3.0",
                "assets": [],
            }
        )

    monkeypatch.setattr(update_checker.requests, "get", _fake_get)
    result = update_checker.get_latest_release_update(
        current_version="1.2.0",
        api_url="https://api.example.com/latest",
        fallback_page_url="https://example.com/releases/latest",
        repo_page_url="https://github.com/org/repo",
    )

    assert result is not None
    assert result["latest_version"] == "v1.3.0"
    assert result["repo_url"] == "https://github.com/org/repo"
    assert result["download_url"] == "https://github.com/org/repo"
