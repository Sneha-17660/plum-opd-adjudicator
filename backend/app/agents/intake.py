"""Intake agent (deterministic): validates uploads and prepares them for OCR."""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field

import fitz

from app.config import Settings


class IntakeError(ValueError):
    """User-facing validation error (HTTP 400)."""


@dataclass
class PreparedDocument:
    file_name: str
    mime: str
    sha256: str
    page_count: int
    images: list[str] = field(default_factory=list)   # data: URLs, JPEG
    text_layer: str = ""


def sniff_mime(content: bytes) -> str | None:
    if content.startswith(b"%PDF"):
        return "application/pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _render(doc: fitz.Document, max_pages: int, longest_side: int = 1800) -> list[str]:
    out = []
    for i in range(min(doc.page_count, max_pages)):
        page = doc.load_page(i)
        rect = page.rect
        scale = min(3.0, longest_side / max(rect.width, rect.height, 1))
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        if pix.colorspace and pix.colorspace.n not in (1, 3):
            pix = fitz.Pixmap(fitz.csRGB, pix)
        out.append("data:image/jpeg;base64," + base64.b64encode(pix.tobytes("jpg", jpg_quality=85)).decode())
    return out


class IntakeAgent:
    name = "Intake Agent"

    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, uploads: list[tuple[str, bytes]]) -> list[PreparedDocument]:
        if not uploads:
            raise IntakeError("Upload at least one document (bill and prescription).")
        if len(uploads) > self.settings.max_files:
            raise IntakeError(f"Upload at most {self.settings.max_files} files per claim.")
        seen: set[str] = set()
        prepared: list[PreparedDocument] = []
        for name, content in uploads:
            name = (name or "document").strip()[:120]
            if not content:
                raise IntakeError(f"{name} is empty.")
            if len(content) > self.settings.max_file_mb * 1024 * 1024:
                raise IntakeError(f"{name} is larger than {self.settings.max_file_mb} MB.")
            mime = sniff_mime(content)
            if mime is None:
                raise IntakeError(f"{name} is not a PDF, JPG, PNG or WEBP file.")
            digest = hashlib.sha256(content).hexdigest()
            if digest in seen:
                continue                      # same file attached twice in one claim
            seen.add(digest)
            try:
                doc = fitz.open(stream=content, filetype="pdf" if mime == "application/pdf" else mime.split("/")[1])
            except Exception as exc:
                raise IntakeError(f"{name} could not be opened; it may be corrupted.") from exc
            try:
                if doc.needs_pass:
                    raise IntakeError(f"{name} is password-protected. Upload an unlocked copy.")
                if doc.page_count == 0:
                    raise IntakeError(f"{name} has no pages.")
                text = ""
                if mime == "application/pdf":
                    text = "\n".join(doc.load_page(i).get_text("text")
                                     for i in range(min(doc.page_count, self.settings.max_pages_per_document)))
                images = _render(doc, self.settings.max_pages_per_document)
                prepared.append(PreparedDocument(name, mime, digest, doc.page_count, images, text.strip()))
            finally:
                doc.close()
        return prepared
