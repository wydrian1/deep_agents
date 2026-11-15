"""``report_create``: private run outputs (md, txt, csv, xlsx, pdf). Nothing is published."""

from __future__ import annotations

import csv
import hashlib
import io
import uuid
from typing import Any

from .. import storage
from .registry import ToolContext, register_tool

KINDS = ("md", "txt", "csv", "xlsx", "pdf")
MAX_FILE = 2 * 1024 * 1024
MAX_FILES, MAX_TOTAL = 10, 10 * 1024 * 1024
MAX_TEXT, MAX_ROWS, MAX_COLS, MAX_CELL, MAX_PDF_PAGES = 64000, 1000, 30, 2000, 100


def _safe_cell(value: object) -> str:
    text = str(value)[:MAX_CELL]
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def render_report(kind: str, text: str, rows: list[list[str]] | None) -> bytes:
    if kind in ("md", "txt"):
        return text.encode("utf-8")
    if kind == "csv":
        text_out = io.StringIO()
        csv.writer(text_out).writerows([[_safe_cell(c) for c in row] for row in rows or []])
        return text_out.getvalue().encode("utf-8")
    if kind == "xlsx":
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        for row in rows or []:
            # Formula-leading cells are neutralised, so no cell becomes a formula/hyperlink.
            sheet.append([_safe_cell(c) for c in row])
        out = io.BytesIO()
        workbook.save(out)
        return out.getvalue()
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas

    out = io.BytesIO()
    pdf = canvas.Canvas(out, pagesize=A4)
    width, height = A4
    y, pages = height - 50, 1
    for paragraph in text.split("\n"):
        for line in simpleSplit(paragraph or " ", "Helvetica", 10, width - 100):
            if y < 50:
                pdf.showPage()
                y, pages = height - 50, pages + 1
                if pages > MAX_PDF_PAGES:
                    raise ValueError("pdf too long")
            pdf.setFont("Helvetica", 10)
            pdf.drawString(50, y, line)
            y -= 13
    pdf.save()
    return out.getvalue()


@register_tool(
    "report", label="Reports (private outputs)", access="private-write",
    description="Create private md/txt/csv/xlsx/pdf outputs for the run (10 files / 10 MB). No overwrite, delete or publishing.",
)
def report_tools(ctx: ToolContext) -> list:
    def report_create(title: str, kind: str, text: str = "", rows: list[list[str]] | None = None) -> dict[str, Any]:
        """Create a private output file. kind: md, txt, pdf (use text) or csv, xlsx (use rows). Returns its id."""
        if (
            not isinstance(title, str) or not 1 <= len(title.strip()) <= 100 or kind not in KINDS
            or not isinstance(text, str) or len(text) > MAX_TEXT
        ):
            return {"error": ctx.t("tool_bad_input")}
        if kind in ("csv", "xlsx"):
            if (
                not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS
                or any(not isinstance(r, list) or len(r) > MAX_COLS for r in rows)
            ):
                return {"error": ctx.t("tool_bad_input")}
        elif not text.strip():
            return {"error": ctx.t("tool_bad_input")}
        try:
            content = render_report(kind, text, rows)
        except ValueError:
            return {"error": ctx.t("tool_bad_input")}
        if len(content) > MAX_FILE:
            return {"error": ctx.t("tool_bad_input")}
        digest = hashlib.sha256(f"{kind}\0{title}\0{text}\0{rows!r}".encode()).hexdigest()
        # Same run + same request = same id, so a replayed call never creates a second copy.
        artifact_id = uuid.uuid5(uuid.UUID(ctx.run_id), digest).hex
        existing = ctx.store.get_artifact(artifact_id)
        if existing:
            return {"id": artifact_id, "name": existing["name"], "title": existing["title"], "size": existing["size"], "reused": True}
        count, total = ctx.store.artifact_usage(ctx.run_id)
        if count >= MAX_FILES or total + len(content) > MAX_TOTAL:
            return {"error": ctx.t("report_limit")}
        path = storage.artifact_path(ctx.settings, ctx.run_id, artifact_id)
        name = f"report-{ctx.run_id[:8]}-{artifact_id[:8]}.{kind}"
        storage.write_exclusive(path, content)
        try:
            ctx.store.add_artifact(artifact_id, ctx.run_id, name, title.strip(), len(content), storage.sha256(content))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return {"id": artifact_id, "name": name, "title": title.strip(), "size": len(content), "private": True}

    return [report_create]
