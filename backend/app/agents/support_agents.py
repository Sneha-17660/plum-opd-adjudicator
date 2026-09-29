"""Eligibility (deterministic), Medical Review (LLM) and Fraud & Risk (deterministic) agents.

These run concurrently once document facts are available. None of them decides
the claim; they produce facts and signals for the policy engine.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from app.agents.prompts import MEDICAL_SYSTEM
from app.config import Settings
from app.llm.groq_client import GroqClient
from app.models import (BILL_TYPES, ClaimForm, DocumentEvidence, HistoryContext, MedicalAssessment,
                        MemberRecord, RiskSignal)
from app.text_utils import norm

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
VALID_CATEGORIES = {"consultation", "diagnostic", "pharmacy", "dental", "vision", "alternative", "other"}

# Deterministic backstop for instructions embedded in documents (the OCR and
# extraction agents also report these). Matching only flags the claim for a
# human; it never changes the decision in the claimant's favour.
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts?)",
    r"ignore\s+(the\s+)?(policy|rules|guidelines)",
    r"\b(ai|assistant|model|system|chatgpt|llm|bot|reviewer|adjudicator)\b.{0,40}\b(must|should|has to|have to|will|shall)\b.{0,20}\bapprove",
    r"\bapprove\s+(this|the)\s+(claim|document|bill)\b",
    r"\b(do\s+not|don'?t|never)\s+(reject|deny|decline)\b",
    r"\boverride\b.{0,30}\b(policy|rules?|decision|limits?)\b",
    r"\b(system|developer)\s+(prompt|message|instruction)",
    r"\bpre-?approved\s+by\s+(ai|the\s+system)\b",
]


def load_members() -> dict[str, MemberRecord]:
    raw = json.loads((DATA_DIR / "members.json").read_text(encoding="utf-8"))["members"]
    return {mid.upper(): MemberRecord(mid.upper(), m["member_name"], m["policy_id"], bool(m["active"]),
                                      date.fromisoformat(m["join_date"]), list(m.get("pre_existing_conditions", [])))
            for mid, m in raw.items()}


def load_preauths() -> list[dict[str, Any]]:
    return json.loads((DATA_DIR / "preauthorizations.json").read_text(encoding="utf-8"))["preauthorizations"]


class EligibilityAgent:
    """Resolves trusted context: member record, pre-authorization and claims history."""
    name = "Eligibility Agent"

    def __init__(self, store):
        self.store = store
        self.members = load_members()
        self.preauths = load_preauths()

    def run(self, form: ClaimForm, docs: list[DocumentEvidence], *, exclude_claim_id: str | None = None
            ) -> tuple[MemberRecord | None, bool, HistoryContext]:
        member = self.members.get(form.member_id.strip().upper())
        refs = {re.sub(r"\s+", "", (d.pre_auth_reference or "")).upper() for d in docs if d.pre_auth_reference}
        preauth_ok = any(p["reference"].upper() in refs and p["member_id"].upper() == form.member_id.upper()
                         and p["valid_from"] <= form.treatment_date.isoformat() <= p["valid_to"]
                         for p in self.preauths)
        bills = [d for d in docs if d.document_type in BILL_TYPES]
        history = self.store.history_context(
            member_id=form.member_id.upper(), treatment_date=form.treatment_date,
            doc_hashes=sorted({d.sha256 for d in docs}),
            invoice_keys=sorted({invoice_key(d) for d in bills if d.invoice_number}),
            exclude_claim_id=exclude_claim_id,
        )
        return member, preauth_ok, history


def invoice_key(doc: DocumentEvidence) -> str:
    return f"{norm(doc.provider_name)}#{norm(doc.invoice_number)}"


class MedicalReviewAgent:
    name = "Medical Review Agent"

    def __init__(self, settings: Settings, client: GroqClient):
        self.settings, self.client = settings, client

    async def review(self, docs: list[DocumentEvidence]) -> MedicalAssessment:
        items = [i.description for d in docs for i in d.line_items]
        case = {
            "diagnosis": [d.diagnosis for d in docs if d.diagnosis],
            "treatments": [t for d in docs for t in d.treatments],
            "medicines": [m for d in docs for m in d.medicines],
            "tests": [t for d in docs for t in d.tests],
            "billed_items": items,
            "patient_age": next((d.patient_age for d in docs if d.patient_age), None),
            "patient_gender": next((d.patient_gender for d in docs if d.patient_gender), None),
        }
        if not case["diagnosis"] and not items:
            return MedicalAssessment(available=False, rationale="No diagnosis or billed items to review.")
        data = await self.client.chat_json(
            model=self.settings.text_model, system=MEDICAL_SYSTEM, max_tokens=1200, agent=self.name,
            content="CASE DATA (JSON, untrusted values):\n" + json.dumps(case, ensure_ascii=False) + "\nReturn the JSON object.")
        consistent = data.get("consistent")
        cats = data.get("item_categories") if isinstance(data.get("item_categories"), dict) else {}
        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        return MedicalAssessment(
            consistent=consistent if isinstance(consistent, bool) else None,
            confidence=confidence,
            concerns=[str(c)[:200] for c in (data.get("concerns") or []) if str(c).strip()][:5],
            rationale=str(data.get("rationale") or "")[:300],
            item_categories={str(k): str(v).lower() for k, v in cats.items() if str(v).lower() in VALID_CATEGORIES},
            available=True,
        )


def scan_injection(text: str) -> list[str]:
    hits = []
    for line in (text or "").splitlines():
        if any(re.search(p, line, flags=re.I) for p in INJECTION_PATTERNS):
            hits.append(line.strip()[:200])
    return hits


class RiskAgent:
    """Fraud & risk signals from trusted history plus what the OCR agent saw."""
    name = "Fraud & Risk Agent"

    def __init__(self, rules: dict[str, Any]):
        self.risk = rules["risk"]

    def assess(self, docs: list[DocumentEvidence], history: HistoryContext) -> list[RiskSignal]:
        signals: list[RiskSignal] = []
        if history.duplicate_of:
            signals.append(RiskSignal("DUPLICATE_CLAIM", "Duplicate claim", "reject",
                                      f"The same bill was already claimed in {', '.join(history.duplicate_of[:3])}."))
        if history.previous_claims_same_day >= self.risk["same_day_previous_claims_threshold"]:
            signals.append(RiskSignal("MULTIPLE_CLAIMS_SAME_DAY", "Multiple claims same day", "review",
                                      f"{history.previous_claims_same_day} other claims exist for this member on the same treatment date."))
        if history.claims_last_30_days >= self.risk["claims_last_30_days_threshold"]:
            signals.append(RiskSignal("HIGH_CLAIM_FREQUENCY", "Unusual pattern detected", "review",
                                      f"{history.claims_last_30_days} claims in the 30 days before this treatment."))
        tamper = sorted({f"{d.file_name}: {t}" for d in docs for t in d.visual_flags})
        if tamper:
            signals.append(RiskSignal("DOCUMENT_TAMPERING", "Possible document tampering", "review", "; ".join(tamper)[:500]))
        injected = set()
        for d in docs:
            injected.update(d.injection_text)
            injected.update(scan_injection(d.transcript))
        if injected:
            signals.append(RiskSignal("PROMPT_INJECTION", "Instructions embedded in document", "review",
                                      "Ignored as untrusted content: " + " | ".join(sorted(injected))[:400]))
        return signals
