"""Tests for main CLI helper functions."""

import argparse
import os
import sys
import types
from datetime import datetime
from pathlib import Path

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Provide a lightweight msal stub so main -> onenote_uploader imports in
# environments where auth dependencies are not installed.
if "msal" not in sys.modules:
    sys.modules["msal"] = types.SimpleNamespace(
        SerializableTokenCache=object,
        PublicClientApplication=object,
    )

from main import (  # noqa: E402
    _determine_mode,
    _run_gui_mode,
    _upload_sections,
    _parse_index_selection,
    _resolve_requested_enex_files,
)
import main as main_module  # noqa: E402
from enex_parser import EvernoteNote  # noqa: E402
from onenote_uploader import OneNoteRequestSizeLimitError  # noqa: E402


def test_parse_index_selection_blank_means_all():
    assert _parse_index_selection("", 4) == [1, 2, 3, 4]
    assert _parse_index_selection("all", 3) == [1, 2, 3]


def test_parse_index_selection_supports_ranges_and_deduplicates():
    assert _parse_index_selection("1,3-5,4", 5) == [1, 3, 4, 5]


def test_parse_index_selection_rejects_invalid_input():
    assert _parse_index_selection("0", 3) is None
    assert _parse_index_selection("2-1", 3) is None
    assert _parse_index_selection("1,a", 3) is None
    assert _parse_index_selection("1-10", 3) is None


def test_resolve_requested_enex_files_matches_with_or_without_extension():
    available = [
        Path("a.enex"),
        Path("b.enex"),
        Path("c.enex"),
    ]

    selected = _resolve_requested_enex_files(
        available,
        ["a", "c.enex", "a.enex"],  # includes duplicate request
    )

    assert selected == [Path("a.enex"), Path("c.enex")]


def test_resolve_requested_enex_files_raises_for_missing():
    available = [Path("a.enex")]
    try:
        _resolve_requested_enex_files(available, ["missing"])
        assert False, "Expected ValueError for missing requested .enex file"
    except ValueError as e:
        assert "missing" in str(e)


def test_determine_mode_gui_default():
    args = argparse.Namespace(
        interactive=False,
        terminal_wizard=False,
        advanced=False,
    )
    assert _determine_mode(args, []) == "gui"


def test_determine_mode_terminal_wizard():
    args = argparse.Namespace(
        interactive=False,
        terminal_wizard=True,
        advanced=False,
    )
    assert _determine_mode(args, ["--terminal-wizard"]) == "terminal_wizard"


def test_determine_mode_advanced():
    args = argparse.Namespace(
        interactive=False,
        terminal_wizard=False,
        advanced=False,
    )
    assert _determine_mode(args, ["--enex-dir", "enex"]) == "advanced"


def test_determine_mode_advanced_flag():
    args = argparse.Namespace(
        interactive=False,
        terminal_wizard=False,
        advanced=True,
    )
    assert _determine_mode(args, ["--advanced"]) == "advanced"


def test_upload_sections_skips_oversized_note_without_aborting(monkeypatch):
    class _Uploader:
        def __init__(self):
            self.created_pages = 0

        def get_or_create_section(self, notebook_id, section_name):
            return "section-1", True

        @staticmethod
        def page_identity_from_note(note):
            return (note.title.strip(), note.created.strftime("%Y-%m-%dT%H:%M:%SZ"))

        def create_page(self, section_id, note, html_body):
            if "big" in note.title.lower():
                raise OneNoteRequestSizeLimitError("too large")
            self.created_pages += 1
            return f"page-{self.created_pages}"

    uploader = _Uploader()
    notes = [
        EvernoteNote(
            title="Big attachment note",
            created=datetime(2025, 1, 1, 0, 0, 0),
            updated=datetime(2025, 1, 1, 0, 0, 0),
            content_enml="<en-note><div>hi</div></en-note>",
        ),
        EvernoteNote(
            title="Small note",
            created=datetime(2025, 1, 1, 0, 1, 0),
            updated=datetime(2025, 1, 1, 0, 1, 0),
            content_enml="<en-note><div>ok</div></en-note>",
        ),
    ]
    sleep_calls: list[float] = []
    monkeypatch.setattr(main_module.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    _upload_sections(
        uploader=uploader,
        notebook_id="notebook-1",
        all_sections={"sec": notes},
        total_notes=2,
    )

    assert uploader.created_pages == 1
    assert sleep_calls == [2.0]


def test_run_gui_mode_no_stdin_shows_error_instead_of_terminal_fallback(monkeypatch):
    class _NoStdin:
        closed = False

        @staticmethod
        def isatty():
            return False

    def _raise_runtime(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setitem(sys.modules, "gui_wizard", types.SimpleNamespace(run_gui_wizard=_raise_runtime))
    monkeypatch.setattr(sys, "stdin", _NoStdin())

    calls = {"interactive": 0, "error": 0}
    monkeypatch.setattr("main._run_interactive_mode", lambda _args: calls.__setitem__("interactive", 1))
    monkeypatch.setattr("main._show_gui_runtime_error", lambda _msg, _err: calls.__setitem__("error", 1))

    args = argparse.Namespace(
        enex_dir=None,
        notebook_name="Notebook",
        client_id=None,
        dry_run=False,
        verbose=False,
    )
    _run_gui_mode(args)

    assert calls["interactive"] == 0
    assert calls["error"] == 1


def test_run_gui_mode_terminal_fallback_when_stdin_available(monkeypatch):
    class _TtyStdin:
        closed = False

        @staticmethod
        def isatty():
            return True

    def _raise_runtime(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setitem(sys.modules, "gui_wizard", types.SimpleNamespace(run_gui_wizard=_raise_runtime))
    monkeypatch.setattr(sys, "stdin", _TtyStdin())

    calls = {"interactive": 0, "error": 0}
    monkeypatch.setattr("main._run_interactive_mode", lambda _args: calls.__setitem__("interactive", 1))
    monkeypatch.setattr("main._show_gui_runtime_error", lambda _msg, _err: calls.__setitem__("error", 1))

    args = argparse.Namespace(
        enex_dir=None,
        notebook_name="Notebook",
        client_id=None,
        dry_run=False,
        verbose=False,
    )
    _run_gui_mode(args)

    assert calls["interactive"] == 1
    assert calls["error"] == 0
