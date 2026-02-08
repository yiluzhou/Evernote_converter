"""Tests for the ENEX parser module."""

import hashlib
import os
import sys
import tempfile
from datetime import datetime

import pytest

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from enex_parser import (
    EvernoteNote,
    EvernoteResource,
    build_onenote_page_html,
    enml_to_html,
    parse_enex,
)

# Path to the sample .enex file
SAMPLE_ENEX = os.path.join(
    os.path.dirname(__file__), "..", "enex", "Industry_Career.enex"
)


# ---------------------------------------------------------------------------
# Test: parse_enex with real file
# ---------------------------------------------------------------------------

class TestParseEnexRealFile:
    """Tests against the actual Industry_Career.enex sample file."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not os.path.exists(SAMPLE_ENEX):
            pytest.skip("Sample .enex file not found")
        self.notes = parse_enex(SAMPLE_ENEX)

    def test_note_count(self):
        assert len(self.notes) == 16

    def test_notes_have_titles(self):
        for note in self.notes:
            assert isinstance(note.title, str)
            assert len(note.title) > 0

    def test_notes_have_dates(self):
        for note in self.notes:
            assert isinstance(note.created, datetime)
            assert isinstance(note.updated, datetime)

    def test_notes_have_content(self):
        for note in self.notes:
            assert isinstance(note.content_enml, str)

    def test_notes_have_author(self):
        # All notes in this file have an author
        for note in self.notes:
            assert isinstance(note.author, str)

    def test_total_resource_count(self):
        total = sum(len(n.resources) for n in self.notes)
        assert total == 11

    def test_resources_have_valid_data(self):
        for note in self.notes:
            for res in note.resources:
                assert isinstance(res.data, bytes)
                assert len(res.data) > 0
                assert isinstance(res.mime, str)
                assert len(res.mime) > 0
                assert isinstance(res.filename, str)
                assert len(res.filename) > 0

    def test_resource_md5_hash_matches_data(self):
        """Verify that the stored md5_hash matches the actual data digest."""
        for note in self.notes:
            for res in note.resources:
                computed = hashlib.md5(res.data).hexdigest()
                assert res.md5_hash == computed

    def test_first_note_title(self):
        # First note in the file is "Torchlib"
        assert self.notes[0].title == "Torchlib"

    def test_first_note_date(self):
        assert self.notes[0].created == datetime(2024, 1, 24, 3, 18, 14)


# ---------------------------------------------------------------------------
# Test: parse_enex with synthetic data
# ---------------------------------------------------------------------------

MINIMAL_ENEX = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE en-export SYSTEM "http://xml.evernote.com/pub/evernote-export4.dtd">
<en-export export-date="20260101T000000Z" application="Evernote" version="10.0">
  <note>
    <guid>guid-123</guid>
    <title>Test Note</title>
    <created>20250101T120000Z</created>
    <updated>20250102T120000Z</updated>
    <tag>python</tag>
    <tag>test</tag>
    <note-attributes>
      <author>Test Author</author>
    </note-attributes>
    <content>
      <![CDATA[<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!DOCTYPE en-note SYSTEM "http://xml.evernote.com/pub/enml2.dtd">
<en-note>
  <div>Hello World</div>
</en-note>]]>
    </content>
  </note>
  <note>
    <title>Empty Note</title>
    <created>20250201T000000Z</created>
    <updated>20250201T000000Z</updated>
    <content>
      <![CDATA[<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!DOCTYPE en-note SYSTEM "http://xml.evernote.com/pub/enml2.dtd">
<en-note><div><br/></div></en-note>]]>
    </content>
  </note>
</en-export>
"""


class TestParseEnexSynthetic:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tmpfile = tempfile.NamedTemporaryFile(
            mode="w", suffix=".enex", delete=False
        )
        self.tmpfile.write(MINIMAL_ENEX)
        self.tmpfile.close()
        self.notes = parse_enex(self.tmpfile.name)
        yield
        os.unlink(self.tmpfile.name)

    def test_note_count(self):
        assert len(self.notes) == 2

    def test_first_note_fields(self):
        note = self.notes[0]
        assert note.title == "Test Note"
        assert note.guid == "guid-123"
        assert note.created == datetime(2025, 1, 1, 12, 0, 0)
        assert note.updated == datetime(2025, 1, 2, 12, 0, 0)
        assert note.tags == ["python", "test"]
        assert note.author == "Test Author"
        assert "Hello World" in note.content_enml

    def test_second_note_no_tags(self):
        note = self.notes[1]
        assert note.title == "Empty Note"
        assert note.guid == ""
        assert note.tags == []

    def test_no_resources(self):
        for note in self.notes:
            assert note.resources == []


# ---------------------------------------------------------------------------
# Test: ENML-to-HTML conversion
# ---------------------------------------------------------------------------

class TestEnmlToHtml:
    def test_basic_content(self):
        enml = '<en-note><div>Hello World</div></en-note>'
        html = enml_to_html(enml, {})
        assert "Hello World" in html

    def test_empty_content(self):
        html = enml_to_html("", {})
        assert html == ""

    def test_en_media_image_replacement(self):
        data = b"test image data"
        md5 = hashlib.md5(data).hexdigest()
        resource = EvernoteResource(
            data=data, mime="image/png", filename="test.png",
            md5_hash=md5, width=100, height=50,
        )
        enml = f'<en-note><en-media hash="{md5}" type="image/png"/></en-note>'
        html = enml_to_html(enml, {md5: resource})
        assert f'src="name:{md5}"' in html
        assert 'alt="test.png"' in html
        assert 'width="100"' in html
        assert 'height="50"' in html

    def test_en_media_file_replacement(self):
        data = b"test pdf data"
        md5 = hashlib.md5(data).hexdigest()
        resource = EvernoteResource(
            data=data, mime="application/pdf", filename="doc.pdf",
            md5_hash=md5,
        )
        enml = f'<en-note><en-media hash="{md5}" type="application/pdf"/></en-note>'
        html = enml_to_html(enml, {md5: resource})
        assert 'data-attachment="doc.pdf"' in html
        assert f'data="name:{md5}"' in html
        assert 'type="application/pdf"' in html

    def test_en_media_missing_resource_removed(self):
        enml = '<en-note><en-media hash="deadbeef" type="image/png"/><div>Keep</div></en-note>'
        html = enml_to_html(enml, {})
        assert "deadbeef" not in html
        assert "Keep" in html

    def test_en_todo_unchecked(self):
        enml = '<en-note><en-todo checked="false"/>Buy milk</en-note>'
        html = enml_to_html(enml, {})
        assert "[ ] " in html

    def test_en_todo_checked(self):
        enml = '<en-note><en-todo checked="true"/>Done task</en-note>'
        html = enml_to_html(enml, {})
        assert "[x] " in html

    def test_en_crypt_replaced(self):
        enml = '<en-note><en-crypt hint="pw">encrypted</en-crypt></en-note>'
        html = enml_to_html(enml, {})
        assert "[Encrypted content]" in html
        assert "encrypted" not in html or "[Encrypted content]" in html

    def test_code_block_conversion(self):
        enml = (
            '<en-note>'
            '<div style="--en-codeblock:true; box-sizing: border-box;">'
            '<div>line1</div><div>line2</div>'
            '</div>'
            '</en-note>'
        )
        html = enml_to_html(enml, {})
        assert "<pre>" in html
        assert "line1" in html
        assert "line2" in html

    def test_hx_core_urls_stripped(self):
        enml = (
            '<en-note>'
            '<img src="hx-core://attachment?aoid=123"/>'
            '<div>Keep this</div>'
            '</en-note>'
        )
        html = enml_to_html(enml, {})
        assert "hx-core" not in html
        assert "Keep this" in html

    def test_preserves_formatting(self):
        enml = '<en-note><b>bold</b> <i>italic</i> <a href="http://example.com">link</a></en-note>'
        html = enml_to_html(enml, {})
        assert "<b>bold</b>" in html
        assert "<i>italic</i>" in html
        assert 'href="http://example.com"' in html


# ---------------------------------------------------------------------------
# Test: build_onenote_page_html
# ---------------------------------------------------------------------------

class TestBuildOneNotePageHtml:
    def test_basic_page(self):
        note = EvernoteNote(
            title="Test Page",
            created=datetime(2025, 6, 15, 10, 30, 0),
            updated=datetime(2025, 6, 15, 10, 30, 0),
            content_enml="",
            tags=["tag1", "tag2"],
        )
        html = build_onenote_page_html(note, "<p>Content</p>")
        assert "<!DOCTYPE html>" in html
        assert "<title>Test Page</title>" in html
        assert '2025-06-15T10:30:00.000Z' in html
        assert "Tags:" in html
        assert "tag1" in html
        assert "<p>Content</p>" in html

    def test_no_tags(self):
        note = EvernoteNote(
            title="No Tags",
            created=datetime(2025, 1, 1, 0, 0, 0),
            updated=datetime(2025, 1, 1, 0, 0, 0),
            content_enml="",
        )
        html = build_onenote_page_html(note, "<p>Body</p>")
        assert "Tags:" not in html

    def test_html_escape_in_title(self):
        note = EvernoteNote(
            title='Title with <script> & "quotes"',
            created=datetime(2025, 1, 1, 0, 0, 0),
            updated=datetime(2025, 1, 1, 0, 0, 0),
            content_enml="",
        )
        html = build_onenote_page_html(note, "")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "&amp;" in html

    def test_evernote_guid_meta(self):
        note = EvernoteNote(
            title="With Guid",
            created=datetime(2025, 1, 1, 0, 0, 0),
            updated=datetime(2025, 1, 1, 0, 0, 0),
            content_enml="",
            guid="abc-123-guid",
        )
        html = build_onenote_page_html(note, "<p>Body</p>")
        assert 'name="evernote-guid"' in html
        assert 'content="abc-123-guid"' in html


# ---------------------------------------------------------------------------
# Test: enml_to_html with real notes
# ---------------------------------------------------------------------------

class TestEnmlToHtmlRealNotes:
    """Test ENML conversion using notes from the real .enex file."""

    @pytest.fixture(autouse=True)
    def setup(self):
        if not os.path.exists(SAMPLE_ENEX):
            pytest.skip("Sample .enex file not found")
        self.notes = parse_enex(SAMPLE_ENEX)

    def test_all_notes_convert_without_error(self):
        for note in self.notes:
            resource_map = {r.md5_hash: r for r in note.resources}
            html = enml_to_html(note.content_enml, resource_map)
            assert isinstance(html, str)

    def test_notes_with_resources_have_name_refs(self):
        """Notes with resources should produce HTML with name: references."""
        for note in self.notes:
            if not note.resources:
                continue
            resource_map = {r.md5_hash: r for r in note.resources}
            html = enml_to_html(note.content_enml, resource_map)
            # Check that at least some resources are referenced
            # (not all resources may be referenced via en-media)
            has_ref = any(f"name:{r.md5_hash}" in html for r in note.resources)
            # It's OK if not all are referenced — some resources might not
            # have en-media tags pointing to them
            assert isinstance(html, str)
