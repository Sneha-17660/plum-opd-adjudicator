"""LLM agents that read documents: OCR (vision) and Extraction (text).

OCR transcribes each file verbatim and reports legibility / tampering signs.
Extraction turns a transcript into structured, per-field-confidence facts.
A deterministic grounding pass then checks that key extracted values really
appear in the document text, so a hallucinated value cannot pass silently.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.agents.intake import PreparedDocument
from app.agents.prompts import EXTRACTION_SYSTEM, OCR_SYSTEM
from app.config import Settings
from app.llm.groq_client import GroqClient, LLMError
from app.models import DocumentEvidence, LineItem
from app.text_utils import norm, parse_date, to_money

VALID_TYPES = {"prescription", "medical_bill", "pharmacy_bill", "diagnostic_report",
               "pre_authorization", "discharge_summary", "other"}
TYPE_ALIASES = {"bill": "medical_bill", "invoice": "medical_bill", "receipt": "medical_bill",
                "hospital_bill": "medical_bill", "lab_report": "diagnostic_report", "report": "diagnostic_report",
                "rx": "prescription", "pharmacy": "pharmacy_bill", "preauth": "pre_authorization"}
CONFIDENCE_FIELDS = ("patient_name", "document_date", "doctor_registration", "diagnosis", "line_items", "total_amount")


@dataclass
class OCRResult:
    transcript: str
    legibility: float
    handwritten: bool
    document_kinds: list[str] = field(default_factory=list)
    tampering_signs: list[str] = field(default_factory=list)
    instruction_like_text: list[str] = field(default_factory=list)
    used_fallback: bool = False


def _f(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip() and str(v).strip().lower() not in {"none", "null", "n/a"}]


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return None if not s or s.lower() in {"null", "none", "n/a", "na", "-", "not mentioned", "unknown"} else s


class OCRAgent:
    name = "OCR Agent"

    def __init__(self, settings: Settings, client: GroqClient):
        self.settings, self.client = settings, client

    async def read(self, doc: PreparedDocument) -> OCRResult:
        content: list[dict[str, Any]] = [{"type": "text", "text":
            f"File: {doc.file_name} ({doc.page_count} page(s); first {len(doc.images)} shown). Transcribe it and return the JSON object."}]
        content += [{"type": "image_url", "image_url": {"url": url}} for url in doc.images]
        try:
            data = await self.client.chat_json(model=self.settings.vision_model, system=OCR_SYSTEM,
                                               content=content, max_tokens=6000, agent=self.name)
        except LLMError:
            # A digital PDF can still be processed from its text layer.
            if len(doc.text_layer) >= 80:
                return OCRResult(doc.text_layer, 0.9, False, [], [], [], used_fallback=True)
            raise
        transcript = str(data.get("transcript") or "").strip()
        if not transcript and doc.text_layer:
            transcript = doc.text_layer
        return OCRResult(
            transcript=transcript,
            legibility=_f(data.get("legibility"), 0.7 if transcript else 0.1),
            handwritten=bool(data.get("handwritten")),
            document_kinds=_str_list(data.get("document_kinds")),
            tampering_signs=_str_list(data.get("tampering_signs")),
            instruction_like_text=_str_list(data.get("instruction_like_text")),
        )


class ExtractionAgent:
    name = "Extraction Agent"

    def __init__(self, settings: Settings, client: GroqClient):
        self.settings, self.client = settings, client

    async def extract(self, doc: PreparedDocument, ocr: OCRResult) -> list[DocumentEvidence]:
        parts = [f"FILE NAME: {doc.file_name}", "OCR TRANSCRIPT (verbatim, untrusted content):", "<<<", ocr.transcript[:20000], ">>>"]
        if doc.text_layer and not ocr.used_fallback:
            parts += ["PDF TEXT LAYER (verbatim, untrusted content):", "<<<", doc.text_layer[:20000], ">>>"]
        parts.append("Return the JSON object described in your instructions.")
        data = await self.client.chat_json(model=self.settings.text_model, system=EXTRACTION_SYSTEM,
                                           content="\n".join(parts), max_tokens=4000, agent=self.name)
        raw_docs = data.get("documents")
        if isinstance(data, dict) and raw_docs is None and "document_type" in data:
            raw_docs = [data]
        if not isinstance(raw_docs, list) or not raw_docs:
            raw_docs = [{"document_type": "other"}]
        source_text = f"{ocr.transcript}\n{doc.text_layer}"
        out = []
        for i, raw in enumerate(x for x in raw_docs if isinstance(x, dict)):
            ev = self._normalise(raw, doc, ocr)
            if len(raw_docs) > 1:
                ev.file_name = f"{doc.file_name} ({ev.document_type.replace('_', ' ')}{'' if i == 0 else f' {i + 1}'})"
            ground(ev, source_text)
            out.append(ev)
        return out

    @staticmethod
    def _normalise(raw: dict[str, Any], doc: PreparedDocument, ocr: OCRResult) -> DocumentEvidence:
        dtype = norm(raw.get("document_type")).replace(" ", "_")
        dtype = TYPE_ALIASES.get(dtype, dtype)
        if dtype not in VALID_TYPES:
            dtype = "other"
        items = []
        for it in raw.get("line_items") or []:
            if not isinstance(it, dict):
                continue
            desc, amt = _clean(it.get("description")), to_money(it.get("amount"))
            if desc and amt and amt > 0 and not re.match(r"(?i)^(sub\s*total|total|grand total|net|amount paid|balance)\b", desc):
                items.append(LineItem(desc, amt))
        conf_raw = raw.get("field_confidence") if isinstance(raw.get("field_confidence"), dict) else {}
        ev = DocumentEvidence(
            file_name=doc.file_name, sha256=doc.sha256, mime=doc.mime, document_type=dtype,
            patient_name=_clean(raw.get("patient_name")), patient_age=_clean(raw.get("patient_age")),
            patient_gender=_clean(raw.get("patient_gender")), provider_name=_clean(raw.get("provider_name")),
            doctor_name=_clean(raw.get("doctor_name")),
            doctor_registration=(re.sub(r"\s+", "", _clean(raw.get("doctor_registration")) or "") or None),
            document_date=parse_date(raw.get("document_date")), follow_up_date=parse_date(raw.get("follow_up_date")),
            diagnosis=_clean(raw.get("diagnosis")), treatments=_str_list(raw.get("treatments")),
            medicines=_str_list(raw.get("medicines")), tests=_str_list(raw.get("tests")),
            line_items=items, total_amount=to_money(raw.get("total_amount")),
            invoice_number=_clean(raw.get("invoice_number")), pre_auth_reference=_clean(raw.get("pre_auth_reference")),
            legibility=ocr.legibility, handwritten=ocr.handwritten,
            visual_flags=list(ocr.tampering_signs),
            injection_text=sorted(set(ocr.instruction_like_text + _str_list(raw.get("instruction_like_text")))),
            transcript=ocr.transcript[:6000], text_layer_chars=len(doc.text_layer),
        )
        present = {"patient_name": ev.patient_name, "document_date": ev.document_date,
                   "doctor_registration": ev.doctor_registration, "diagnosis": ev.diagnosis,
                   "line_items": ev.line_items, "total_amount": ev.total_amount}
        for key in CONFIDENCE_FIELDS:
            if present[key]:
                ev.field_confidence[key] = _f(conf_raw.get(key), 0.7)
        if raw.get("document_date") and not ev.document_date:
            ev.extraction_warnings.append(f"Unreadable date '{raw.get('document_date')}'")
        return ev


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def ground(ev: DocumentEvidence, source_text: str) -> None:
    """Lower confidence for values that don't literally appear in the source text."""
    src = source_text.upper()
    src_compact = re.sub(r"\s+", "", src)
    src_digits = re.sub(r"[,\s]", "", src)

    def miss(key: str, what: str) -> None:
        ev.field_confidence[key] = min(ev.field_confidence.get(key, 0.7), 0.4)
        ev.extraction_warnings.append(f"{what} not found verbatim in the document text")

    if ev.doctor_registration and re.sub(r"\s+", "", ev.doctor_registration.upper()) not in src_compact:
        miss("doctor_registration", f"Registration '{ev.doctor_registration}'")
    if ev.total_amount:
        whole = str(int(ev.total_amount)) if float(ev.total_amount).is_integer() else f"{ev.total_amount:.2f}"
        if whole not in src_digits:
            miss("total_amount", f"Total {whole}")
    for it in ev.line_items:
        amt = str(int(it.amount)) if float(it.amount).is_integer() else f"{it.amount:.2f}"
        if amt not in src_digits:
            miss("line_items", f"Line item amount {amt}")
            break
    if ev.patient_name:
        tokens = [t for t in norm(ev.patient_name).split() if len(t) > 2]
        if tokens and not any(t.upper() in src for t in tokens):
            miss("patient_name", f"Patient name '{ev.patient_name}'")


def document_confidence(ev: DocumentEvidence) -> float:
    """Mean field confidence, tempered by legibility."""
    confs = list(ev.field_confidence.values())
    base = sum(confs) / len(confs) if confs else 0.6
    return round(base * (0.7 + 0.3 * ev.legibility), 3)


async def gather_limited(limit: int, coros: list) -> list:
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c
    return await asyncio.gather(*(run(c) for c in coros))


def summarise_docs(docs: list[DocumentEvidence]) -> str:
    return json.dumps([{"file": d.file_name, "type": d.document_type} for d in docs])
