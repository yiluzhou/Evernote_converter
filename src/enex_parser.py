"""
ENEX Parser — Phase 1 of Evernote-to-OneNote Converter

Parses Evernote .enex export files (XML) and converts ENML content to
OneNote-compatible HTML. Handles notes, resources/attachments, and the
en-media hash linking mechanism.

Reference:
  - ENML spec: https://dev.evernote.com/doc/articles/enml.php
  - ENML_PY: https://github.com/CarlLee/ENML_PY
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from html import escape as html_escape

from bs4 import BeautifulSoup
from lxml import etree


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class EvernoteResource:
    """A binary resource (image, file attachment) from an Evernote note."""
    data: bytes
    mime: str
    filename: str
    md5_hash: str
    width: int | None = None
    height: int | None = None


@dataclass
class EvernoteNote:
    """A single note parsed from an .enex file."""
    title: str
    created: datetime
    updated: datetime
    content_enml: str
    guid: str = ""
    tags: list[str] = field(default_factory=list)
    resources: list[EvernoteResource] = field(default_factory=list)
    author: str = ""


# ---------------------------------------------------------------------------
# ENEX parsing
# ---------------------------------------------------------------------------

def parse_enex(filepath: str) -> list[EvernoteNote]:
    """
    Parse a .enex file and return a list of EvernoteNote objects.

    Uses lxml iterparse for memory-efficient streaming of large files.
    """
    notes: list[EvernoteNote] = []
    parse_kwargs: dict[str, object] = {
        "events": ("end",),
        "tag": "note",
    }
    try:
        iterator = etree.iterparse(filepath, huge_tree=True, **parse_kwargs)
    except TypeError:
        # Older lxml may not support huge_tree; keep compatibility fallback.
        iterator = etree.iterparse(filepath, **parse_kwargs)

    for _event, elem in iterator:
        note = _parse_note_element(elem)
        notes.append(note)
        # Free memory — important for large exports
        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]
    return notes


def _parse_note_element(elem: etree._Element) -> EvernoteNote:
    """Extract note data from a <note> XML element."""
    title = elem.findtext("title", default="Untitled")
    guid = (elem.findtext("guid", default="") or "").strip()
    created = _parse_evernote_date(elem.findtext("created", ""))
    updated = _parse_evernote_date(elem.findtext("updated", ""))
    content_enml = elem.findtext("content", default="")
    tags = [t.text for t in elem.findall("tag") if t.text]

    author = ""
    note_attrs = elem.find("note-attributes")
    if note_attrs is not None:
        author = note_attrs.findtext("author", default="")

    resources: list[EvernoteResource] = []
    for res_elem in elem.findall("resource"):
        resource = _parse_resource_element(res_elem)
        if resource is not None:
            resources.append(resource)

    return EvernoteNote(
        title=title,
        created=created,
        updated=updated,
        content_enml=content_enml,
        guid=guid,
        tags=tags,
        resources=resources,
        author=author,
    )


def _parse_resource_element(elem: etree._Element) -> EvernoteResource | None:
    """Extract resource data from a <resource> XML element."""
    data_elem = elem.find("data")
    if data_elem is None or not data_elem.text:
        return None

    raw_data = base64.b64decode(data_elem.text)
    md5_hash = hashlib.md5(raw_data).hexdigest()
    mime = elem.findtext("mime", default="application/octet-stream")

    filename = ""
    res_attrs = elem.find("resource-attributes")
    if res_attrs is not None:
        filename = res_attrs.findtext("file-name", default="")
    if not filename:
        ext = _mime_to_extension(mime)
        filename = f"{md5_hash}.{ext}"

    width = _int_or_none(elem.findtext("width"))
    height = _int_or_none(elem.findtext("height"))

    return EvernoteResource(
        data=raw_data,
        mime=mime,
        filename=filename,
        md5_hash=md5_hash,
        width=width,
        height=height,
    )


def _parse_evernote_date(date_str: str) -> datetime:
    """Parse Evernote date format '20240124T031814Z' to datetime."""
    if not date_str:
        return datetime.now()
    try:
        return datetime.strptime(date_str, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return datetime.now()


def _int_or_none(val: str | None) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except ValueError:
        return None


def _mime_to_extension(mime: str) -> str:
    """Convert a MIME type to a file extension."""
    mime_map = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/bmp": "bmp",
        "image/svg+xml": "svg",
        "application/pdf": "pdf",
        "audio/mpeg": "mp3",
        "audio/wav": "wav",
        "text/plain": "txt",
        "text/html": "html",
    }
    if mime in mime_map:
        return mime_map[mime]
    # Fallback: use the subtype portion
    parts = mime.split("/")
    if len(parts) == 2:
        return parts[1].split(";")[0].split("+")[0]
    return "bin"


# ---------------------------------------------------------------------------
# ENML-to-HTML conversion
# ---------------------------------------------------------------------------

def enml_to_html(
    enml_content: str,
    resource_map: dict[str, EvernoteResource],
) -> str:
    """
    Convert ENML content to HTML suitable for OneNote page creation.

    Transformations:
      - <en-note> root -> bare content (caller wraps in full HTML page)
      - <en-media hash="..." type="..."/> -> <img> (images) or <object> (files)
      - <en-todo checked="true/false"/> -> checkbox text
      - <en-crypt> -> placeholder text
      - code blocks (--en-codeblock:true style) -> <pre>
      - hx-core:// image URLs -> stripped (Evernote-internal references)

    Args:
        enml_content: raw ENML string from <content> CDATA
        resource_map: {md5_hash: EvernoteResource} lookup

    Returns:
        HTML body content string with name: references for multipart upload
    """
    if not enml_content.strip():
        return ""

    # Strip XML declaration and DOCTYPE if present — they confuse HTML parser
    content = re.sub(r"<\?xml[^?]*\?>", "", enml_content)
    content = re.sub(r"<!DOCTYPE[^>]*>", "", content)

    soup = BeautifulSoup(content, "html.parser")

    # Replace <en-media> with appropriate HTML elements
    for media_tag in soup.find_all("en-media"):
        hash_val = media_tag.get("hash", "")
        mime_type = media_tag.get("type", "")
        resource = resource_map.get(hash_val)

        if resource is None:
            media_tag.decompose()
            continue

        if mime_type.startswith("image/"):
            img = soup.new_tag(
                "img",
                src=f"name:{resource.md5_hash}",
                alt=resource.filename,
            )
            if resource.width:
                img["width"] = str(resource.width)
            if resource.height:
                img["height"] = str(resource.height)
            media_tag.replace_with(img)
        else:
            obj = soup.new_tag(
                "object",
                **{
                    "data-attachment": resource.filename,
                    "data": f"name:{resource.md5_hash}",
                    "type": resource.mime,
                },
            )
            obj.string = resource.filename
            media_tag.replace_with(obj)

    # Replace <en-todo> with checkbox text
    for todo in soup.find_all("en-todo"):
        checked = todo.get("checked", "false") == "true"
        checkbox = "[x] " if checked else "[ ] "
        todo.replace_with(checkbox)

    # Remove <en-crypt> blocks (cannot decrypt without user password)
    for crypt in soup.find_all("en-crypt"):
        crypt.replace_with("[Encrypted content]")

    # Convert code blocks: div with --en-codeblock style to <pre>
    for div in soup.find_all(
        "div", style=lambda s: s and "--en-codeblock:true" in s
    ):
        pre = soup.new_tag("pre")
        # Collect text from child divs (each line is a <div>)
        lines = []
        for child in div.find_all("div"):
            lines.append(child.get_text())
        if lines:
            pre.string = "\n".join(lines)
        else:
            pre.string = div.get_text()
        div.replace_with(pre)

    # Strip hx-core:// image URLs (Evernote-internal references)
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src.startswith("hx-core://"):
            img.decompose()

    # Extract content from <en-note> root
    en_note = soup.find("en-note")
    if en_note:
        return en_note.decode_contents()
    body = soup.find("body")
    if body:
        return body.decode_contents()
    return str(soup)


def build_onenote_page_html(
    note: EvernoteNote,
    html_body: str,
) -> str:
    """
    Wrap converted HTML body in a full OneNote page HTML structure.

    Returns the complete HTML string ready for Graph API submission.
    """
    created_iso = note.created.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    title = html_escape(note.title)
    guid_meta = ""
    if note.guid:
        guid_meta = (
            f'  <meta name="evernote-guid" content="{html_escape(note.guid)}" />\n'
        )

    tag_line = ""
    if note.tags:
        escaped_tags = ", ".join(html_escape(t) for t in note.tags)
        tag_line = f"<p><b>Tags:</b> {escaped_tags}</p><hr/>"

    return (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "<head>\n"
        f"  <title>{title}</title>\n"
        f'  <meta name="created" content="{created_iso}" />\n'
        f"{guid_meta}"
        "</head>\n"
        "<body>\n"
        f"{tag_line}\n"
        f"{html_body}\n"
        "</body>\n"
        "</html>"
    )
