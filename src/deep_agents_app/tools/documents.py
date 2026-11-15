"""Tools over files attached to the run: listing, raw reading, and text extraction.

Parsing happens IN-PROCESS with bounded, XXE-safe parsers. That is not a sandbox; a
hostile document could still exploit a parser bug. Moving the parsers into a separate
resource-limited process is a planned hardening step (see docs/ARCHITECTURE.md).
OCR/image documents are not supported.
"""

from __future__ import annotations

import base64
import io
import re
import zipfile
from typing import Any

from defusedxml import ElementTree

from .. import storage
from .registry import ToolContext, register_tool

PAGE = 32000
MAX_TEXT = 120000
MAX_PDF_PAGES = 100
MAX_ZIP_ENTRIES = 2000
MAX_ZIP_EXPANDED = 32 * 1024 * 1024
MAX_ZIP_ENTRY = 8 * 1024 * 1024
MAX_RATIO = 500
MAX_SHEETS, MAX_ROWS, MAX_COLS = 20, 2000, 50


class Unsupported(Exception):
    pass


def _open_zip(content: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise Unsupported from None
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_ZIP_EXPANDED:
        raise Unsupported
    for info in infos:
        if info.file_size > MAX_ZIP_ENTRY or (info.compress_size and info.file_size / info.compress_size > MAX_RATIO):
            raise Unsupported
    return archive


def _xml_text(archive: zipfile.ZipFile, name: str, tag: str) -> str:
    root = ElementTree.fromstring(archive.read(name))
    return " ".join((node.text or "") for node in root.iter() if node.tag.endswith("}" + tag))


def extract_text(name: str, content: bytes) -> str:
    lower = name.lower()
    try:
        if lower.endswith(".pdf"):
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted or len(reader.pages) > MAX_PDF_PAGES:
                raise Unsupported
            text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
        elif lower.endswith(".docx"):
            with _open_zip(content) as archive:
                text = _xml_text(archive, "word/document.xml", "t")
        elif lower.endswith(".pptx"):
            with _open_zip(content) as archive:
                slides = sorted(
                    (n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                    key=lambda n: int(re.findall(r"\d+", n)[0]),
                )[:100]
                text = "\n\n".join(_xml_text(archive, n, "t") for n in slides)
        elif lower.endswith(".xlsx"):
            _open_zip(content).close()
            from openpyxl import load_workbook

            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            lines = []
            for sheet in workbook.worksheets[:MAX_SHEETS]:
                lines.append(f"## {sheet.title}")
                for row in sheet.iter_rows(max_row=MAX_ROWS, max_col=MAX_COLS, values_only=True):
                    lines.append("\t".join("" if cell is None else str(cell) for cell in row))
            text = "\n".join(lines)
        else:
            text = content.decode("utf-8")
            if "\x00" in text:
                raise Unsupported
    except Unsupported:
        raise
    except Exception:
        raise Unsupported from None
    return text[:MAX_TEXT]


@register_tool(
    "files", label="Attached files", access="read",
    description="List and read ONLY the files attached to this run (raw bytes/text, bounded).",
)
def files_tools(ctx: ToolContext) -> list:
    def rows() -> dict[str, dict[str, Any]]:
        return {row["id"]: row for row in ctx.store.get_input_files(ctx.file_ids)}

    def file_list() -> list[dict[str, Any]]:
        """List the files attached to this task (ids, names and sizes)."""
        return [{"id": r["id"], "name": r["name"], "size": r["size"]} for r in rows().values()]

    def file_read(file_id: str, offset: int = 0, limit: int = 16000) -> dict[str, Any]:
        """Read an attached file by id with a byte offset/limit. UTF-8 text, or base64 for binary files."""
        attached = rows()
        if file_id not in attached or isinstance(offset, bool) or isinstance(limit, bool) or offset < 0 or not 1 <= limit <= 32000:
            return {"error": ctx.t("tool_bad_input")}
        row = attached[file_id]
        try:
            content = storage.read_checked(
                storage.input_path(ctx.settings, file_id), row["size"], row["sha256"], storage.MAX_UPLOAD_BYTES
            )[offset : offset + limit]
        except (ValueError, OSError):
            return {"error": ctx.t("file_unreadable")}
        try:
            value, encoding = content.decode("utf-8"), "utf-8"
            if "\x00" in value:
                raise UnicodeError
        except UnicodeError:
            value, encoding = base64.b64encode(content).decode("ascii"), "base64"
        return {"name": row["name"], "offset": offset, "encoding": encoding, "content": value}

    return [file_list, file_read]


@register_tool(
    "document", label="Documents (PDF, Office, text)", access="read", requires=("files",),
    description="Extract text from attached PDF/DOCX/PPTX/XLSX/UTF-8 files in bounded pages. In-process parsing; no OCR.",
)
def document_tools(ctx: ToolContext) -> list:
    def document_read(file_id: str, page: int = 0) -> dict[str, Any]:
        """Extract text from an attached PDF, DOCX, PPTX, XLSX or text file; returns one 32000-character page."""
        attached = {row["id"]: row for row in ctx.store.get_input_files(ctx.file_ids)}
        if file_id not in attached or isinstance(page, bool) or page < 0:
            return {"error": ctx.t("tool_bad_input")}
        row = attached[file_id]
        try:
            content = storage.read_checked(
                storage.input_path(ctx.settings, file_id), row["size"], row["sha256"], storage.MAX_UPLOAD_BYTES
            )
        except (ValueError, OSError):
            return {"error": ctx.t("file_unreadable")}
        try:
            text = extract_text(row["name"], content)
        except Unsupported:
            return {"error": ctx.t("document_unsupported")}
        chunk = text[page * PAGE : (page + 1) * PAGE]
        return {
            "name": row["name"],
            "page": page,
            "pages": max(1, -(-len(text) // PAGE)),
            "truncated_at_limit": len(text) >= MAX_TEXT,
            "text": chunk,
        }

    return [document_read]
