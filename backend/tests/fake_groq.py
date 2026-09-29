"""A stand-in for the Groq API used by the end-to-end tests.

It never sees the real model; it answers the three agent prompts from the
known layout of the generated sample documents. This lets the tests exercise
the real HTTP client, JSON parsing, orchestration, persistence and API while
staying offline and deterministic.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import httpx

TRANSCRIPTS: dict[str, str] = {}   # file name -> text the "vision model" should read
CALLS: list[str] = []


def _iso(d: str) -> str:
    return datetime.strptime(d.strip(), "%d/%m/%Y").date().isoformat()


def _after(lines: list[str], prefix: str) -> str | None:
    for ln in lines:
        if ln.startswith(prefix):
            return ln[len(prefix):].strip()
    return None


def parse_sample(text: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    provider = lines[0]
    if "BILL / RECEIPT" in lines:
        start, end = lines.index("Amount (Rs.)") + 1, lines.index("Total Amount")
        rows = lines[start:end]
        items = [{"description": rows[i + 1], "amount": float(rows[i + 2].replace(",", ""))}
                 for i in range(0, len(rows) - 2, 3)]
        total = float(lines[end + 1].replace("Rs.", "").replace(",", "").strip())
        return {"document_type": "medical_bill", "provider_name": provider,
                "patient_name": _after(lines, "Patient Name:"), "document_date": _iso(_after(lines, "Bill Date:")),
                "invoice_number": _after(lines, "Bill No:"), "line_items": items, "total_amount": total,
                "field_confidence": {"patient_name": 0.97, "document_date": 0.97, "line_items": 0.96, "total_amount": 0.97}}
    rx_lines = [re.sub(r"^\d+\.\s*", "", ln) for ln in lines if re.match(r"^\d+\.\s", ln)]
    meds, tests, treatments = [], [], []
    for ln in rx_lines:
        if ln.startswith("Procedure:"):
            treatments.append(ln.split(":", 1)[1].strip())
        elif ln.startswith("Investigations:"):
            tests += [t.strip() for t in ln.split(":", 1)[1].split(",")]
        elif ln.startswith("MRI"):
            tests.append(ln)
        elif re.match(r"^(Tab|Cap|Syp)\.", ln) or " mg" in ln:
            meds.append(ln)
        else:
            treatments.append(ln)
    diag_idx = lines.index("Diagnosis:")
    follow = _after(lines, "Next visit / follow-up:")
    return {"document_type": "prescription", "provider_name": provider, "doctor_name": lines[3],
            "doctor_registration": _after(lines, "Reg. No.:"), "document_date": _iso(_after(lines, "Date:")),
            "follow_up_date": _iso(follow) if follow else None,
            "patient_name": _after(lines, "Patient:"), "diagnosis": lines[diag_idx + 1],
            "treatments": treatments, "medicines": meds, "tests": tests,
            "field_confidence": {"patient_name": 0.97, "document_date": 0.96, "doctor_registration": 0.98, "diagnosis": 0.95}}


def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    system = body["messages"][0]["content"]
    user = body["messages"][1]["content"]
    if system.startswith("You are the OCR agent"):
        CALLS.append("ocr")
        name = re.search(r"File: (.+?) \(", user[0]["text"]).group(1)
        text = TRANSCRIPTS[name]
        injected = [ln.strip() for ln in text.splitlines() if "AI" in ln and "approve" in ln]
        reply = {"transcript": text, "legibility": 0.95, "handwritten": False, "document_kinds": [],
                 "tampering_signs": [], "instruction_like_text": injected, "language": "en"}
        content = json.dumps(reply)
    elif system.startswith("You are the Extraction agent"):
        CALLS.append("extraction")
        transcript = user.split("<<<\n", 1)[1].split("\n>>>", 1)[0]
        # Wrap like a reasoning model would, to exercise the parser.
        content = "<think>reading the bill</think>\n```json\n" + json.dumps({"documents": [parse_sample(transcript)]}) + "\n```"
    else:
        CALLS.append("medical")
        content = json.dumps({"consistent": True, "confidence": 0.9, "concerns": [],
                              "rationale": "Standard care for the diagnosis.", "item_categories": {}})
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


transport = httpx.MockTransport(handler)
