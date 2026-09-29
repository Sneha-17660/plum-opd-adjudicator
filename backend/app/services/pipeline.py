"""Claim orchestrator: coordinates the agents and hands facts to the policy engine.

    Intake ─▶ OCR (per file, parallel) ─▶ Extraction (per file, parallel)
          ─▶ [Eligibility ‖ Medical Review ‖ Fraud & Risk] ─▶ Policy Engine ─▶ Store

Every step is recorded in the claim's trace so a reviewer can see which agent
produced which fact and how long it took.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any

from app.agents.base import Trace
from app.agents.document_agents import (ExtractionAgent, OCRAgent, OCRResult, document_confidence, gather_limited)
from app.agents.intake import IntakeAgent
from app.agents.support_agents import EligibilityAgent, MedicalReviewAgent, RiskAgent, invoice_key
from app.config import Settings
from app.engine.policy_engine import PolicyEngine
from app.llm.groq_client import GroqClient, LLMError
from app.models import BILL_TYPES, PRESCRIPTION_TYPES, ClaimFacts, ClaimForm, DocumentEvidence, MedicalAssessment, to_jsonable
from app.services.store import ClaimStore

LLM_CONCURRENCY = 3


async def _timed(coro) -> tuple[Any, int]:
    started = time.perf_counter()
    result = await coro
    return result, int((time.perf_counter() - started) * 1000)


def _medical_summary(m: MedicalAssessment) -> str:
    if not m.available:
        return m.rationale or "Unavailable."
    if m.consistent is False:
        return f"Concerns: {'; '.join(m.concerns) or 'see rationale'}. {m.rationale}".strip()
    if m.consistent:
        return f"Treatment is consistent with the diagnosis. {m.rationale}".strip()
    return f"Not enough information to judge. {m.rationale}".strip()


def new_claim_id() -> str:
    return f"CLM_{uuid.uuid4().hex[:8].upper()}"


def extraction_confidence(docs: list[DocumentEvidence]) -> float:
    """Confidence in the facts the decision depends on (prescriptions and bills weigh most)."""
    if not docs:
        return 0.0
    weighted = [(document_confidence(d), 2.0 if d.document_type in PRESCRIPTION_TYPES | BILL_TYPES else 0.5) for d in docs]
    return round(sum(c * w for c, w in weighted) / sum(w for _, w in weighted), 2)


class ClaimPipeline:
    def __init__(self, settings: Settings, store: ClaimStore, engine: PolicyEngine, client: GroqClient | None = None):
        self.settings, self.store, self.engine = settings, store, engine
        client = client or GroqClient(settings)
        self.intake = IntakeAgent(settings)
        self.ocr = OCRAgent(settings, client)
        self.extractor = ExtractionAgent(settings, client)
        self.eligibility = EligibilityAgent(store)
        self.medical = MedicalReviewAgent(settings, client)
        self.risk = RiskAgent(engine.rules)

    async def process(self, form: ClaimForm, uploads: list[tuple[str, bytes]], *, submission_date: date | None = None) -> dict[str, Any]:
        trace = Trace()
        submission_date = submission_date or date.today()
        claim_id = new_claim_id()

        t0 = time.perf_counter()
        prepared = await asyncio.to_thread(self.intake.run, uploads)
        trace.add("Intake Agent", "deterministic", "ok", t0,
                  f"Accepted {len(prepared)} file(s); rendered {sum(len(p.images) for p in prepared)} page image(s).")

        # OCR every file in parallel (vision model).
        t0 = time.perf_counter()
        ocr_results: list[OCRResult] = await gather_limited(LLM_CONCURRENCY, [self.ocr.read(p) for p in prepared])
        fallback = [p.file_name for p, o in zip(prepared, ocr_results) if o.used_fallback]
        trace.add("OCR Agent", "llm", "warning" if fallback else "ok", t0,
                  f"Transcribed {len(prepared)} file(s); lowest legibility {min(o.legibility for o in ocr_results):.0%}."
                  + (f" Vision OCR unavailable for {', '.join(fallback)}; used the PDF text layer." if fallback else ""),
                  model=self.settings.vision_model)

        # Structured extraction per file in parallel (text model).
        t0 = time.perf_counter()
        per_file = await gather_limited(LLM_CONCURRENCY, [self.extractor.extract(p, o) for p, o in zip(prepared, ocr_results)])
        docs: list[DocumentEvidence] = [d for group in per_file for d in group]
        warnings = [w for d in docs for w in d.extraction_warnings]
        trace.add("Extraction Agent", "llm", "warning" if warnings else "ok", t0,
                  "Found " + ", ".join(f"{d.document_type.replace('_', ' ')} ({d.file_name})" for d in docs)
                  + (f". Grounding: {'; '.join(warnings[:3])}." if warnings else "."), model=self.settings.text_model)

        # Eligibility, medical review and risk run concurrently.
        (elig, elig_ms), (medical, med_ms) = await asyncio.gather(
            _timed(asyncio.to_thread(self.eligibility.run, form, docs)), _timed(self._medical(docs)))
        member, preauth_ok, history = elig
        trace.add_ms("Eligibility Agent", "deterministic", "ok" if member else "warning", elig_ms,
                     (f"Member {member.member_id} found (joined {member.join_date.isoformat()})." if member
                      else f"Member {form.member_id} is not in the registry.")
                     + f" {history.previous_claims_same_day} other claim(s) on that date; ₹{history.ytd_paid:,.0f} paid this year.")
        trace.add_ms("Medical Review Agent", "llm", "ok" if medical.available else "warning", med_ms, _medical_summary(medical),
                     model=self.settings.text_model)
        t1 = time.perf_counter()
        signals = self.risk.assess(docs, history)
        trace.add("Fraud & Risk Agent", "deterministic", "warning" if signals else "ok", t1,
                  "; ".join(s.label for s in signals) if signals else "No fraud, tampering or injection signals.")

        facts = ClaimFacts(form=form, member=member, documents=docs, history=history, medical=medical,
                           risk_signals=signals, preauth_valid=preauth_ok, submission_date=submission_date,
                           extraction_confidence=extraction_confidence(docs))
        t1 = time.perf_counter()
        result = self.engine.adjudicate(facts, claim_id=claim_id)
        trace.add("Policy Engine", "deterministic", "ok", t1,
                  f"{result['decision']} — ₹{result['approved_amount']:,.0f} of ₹{result['claimed_amount']:,.0f}.")
        return self._finalise(result, facts, trace)

    async def _medical(self, docs: list[DocumentEvidence]) -> MedicalAssessment:
        try:
            return await self.medical.review(docs)
        except LLMError as exc:          # advisory only: the claim proceeds without it
            return MedicalAssessment(available=False, rationale=f"Medical review unavailable: {exc}")

    def _finalise(self, result: dict[str, Any], facts: ClaimFacts, trace: Trace) -> dict[str, Any]:
        facts_json = to_jsonable(facts)
        bills = [d for d in facts.documents if d.document_type in BILL_TYPES]
        result.update({
            "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "form": facts_json["form"],
            "member": facts_json["member"],
            "provider": next((d.provider_name for d in bills if d.provider_name), None),
            "documents": [{k: v for k, v in doc.items() if k not in {"transcript"}} | {"transcript_preview": doc["transcript"][:1200]}
                          for doc in facts_json["documents"]],
            "history": facts_json["history"],
            "medical_review": facts_json["medical"],
            "extraction_confidence": facts.extraction_confidence,
            "trace": to_jsonable(trace.entries),
            "final_authority": "Policy Engine (deterministic)",
        })
        self.store.save(result, facts_json,
                        doc_hashes=sorted({d.sha256 for d in facts.documents}),
                        invoice_keys=sorted({invoice_key(d) for d in bills if d.invoice_number}))
        return result
