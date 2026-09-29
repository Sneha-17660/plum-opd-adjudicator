"""Deterministic policy adjudication engine.

This is the only component allowed to decide a claim. It is a pure function of
``ClaimFacts`` + the policy configuration: no network calls, no LLM, no clock
(the submission date arrives as data). The steps follow
``data/adjudication_rules.md`` in order.

LLM-derived signals (medical consistency, visual tampering, injected
instructions) can only *escalate* a claim to MANUAL_REVIEW; they can never
approve it or reject it on their own.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.models import BILL_TYPES, PRESCRIPTION_TYPES, ClaimFacts, DocumentEvidence, LineItem
from app.text_utils import doctor_registration_valid, first_match, has_phrase, names_match, norm

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# Rejections whose trigger depends on OCR-read content (vs. registry/form data).
# If extraction confidence is low, these become MANUAL_REVIEW instead of REJECTED.
EVIDENCE_DEPENDENT_CODES = {
    "MISSING_DOCUMENTS", "DOCTOR_REG_INVALID", "DATE_MISMATCH", "PATIENT_MISMATCH",
    "SERVICE_NOT_COVERED", "EXCLUDED_CONDITION", "PRE_AUTH_MISSING", "WAITING_PERIOD",
    "PER_CLAIM_EXCEEDED",
}
NEXT_STEPS = {
    "MEMBER_NOT_COVERED": "Check the member ID on the claim form against your policy card.",
    "PATIENT_MISMATCH": "Submit documents issued in the insured member's name, or correct the claim form.",
    "POLICY_INACTIVE": "Contact your HR/policy administrator to confirm the policy status on the treatment date.",
    "WAITING_PERIOD": "This condition becomes claimable after the waiting period ends.",
    "MISSING_DOCUMENTS": "Upload both the original bill and the prescription from a registered doctor.",
    "ILLEGIBLE_DOCUMENTS": "Re-upload clearer scans or photos of the documents.",
    "DOCTOR_REG_INVALID": "Upload a prescription that shows the doctor's registration number (State/Number/Year).",
    "DATE_MISMATCH": "Make sure the bill and prescription are for the treatment date on the claim.",
    "SERVICE_NOT_COVERED": "This treatment is excluded under the policy and cannot be reimbursed.",
    "EXCLUDED_CONDITION": "This condition is excluded under the policy and cannot be reimbursed.",
    "PRE_AUTH_MISSING": "Obtain pre-authorization before MRI/CT procedures and include the reference.",
    "PER_CLAIM_EXCEEDED": "Split separate treatments into separate claims, each within the per-claim limit.",
    "ANNUAL_LIMIT_EXCEEDED": "The annual OPD limit for this member has been used up.",
    "BELOW_MIN_AMOUNT": "Claims must be at least the policy minimum amount.",
    "LATE_SUBMISSION": "Claims must be submitted within the policy's submission window.",
    "DUPLICATE_CLAIM": "This bill has already been claimed; check your claim history.",
}


def load_policy() -> dict[str, Any]:
    return json.loads((DATA_DIR / "policy_terms.json").read_text(encoding="utf-8"))


def load_rules() -> dict[str, Any]:
    return json.loads((DATA_DIR / "coverage_rules.json").read_text(encoding="utf-8"))


def inr(value: float) -> str:
    value = round(float(value), 2)
    return f"₹{value:,.0f}" if value.is_integer() else f"₹{value:,.2f}"


@dataclass
class _Ctx:
    checks: list[dict[str, Any]] = field(default_factory=list)
    reject: list[tuple[str, str]] = field(default_factory=list)   # (code, human reason)
    review: list[tuple[str, str]] = field(default_factory=list)   # (code, human reason)
    partial: list[tuple[str, str]] = field(default_factory=list)  # (code, human reason)
    uncertainty: list[str] = field(default_factory=list)

    def check(self, step: str, name: str, status: str, detail: str, code: str | None = None) -> None:
        self.checks.append({"step": step, "name": name, "status": status, "detail": detail, "code": code})

    def fail(self, step: str, name: str, code: str, detail: str) -> None:
        self.check(step, name, "fail", detail, code)
        if code not in {c for c, _ in self.reject}:
            self.reject.append((code, detail))

    def escalate(self, step: str, name: str, code: str, detail: str) -> None:
        self.check(step, name, "warn", detail, code)
        if code not in {c for c, _ in self.review}:
            self.review.append((code, detail))


class PolicyEngine:
    def __init__(self, policy: dict[str, Any] | None = None, rules: dict[str, Any] | None = None):
        self.policy = policy or load_policy()
        self.rules = rules or load_rules()
        self.cov = self.policy["coverage_details"]
        self.risk = self.rules["risk"]

    # ------------------------------------------------------------------ helpers
    def categorise(self, text: str) -> str | None:
        kw = self.rules["category_keywords"]
        for cat in self.rules["category_order"]:
            if first_match(text, kw[cat]):
                return cat
        return None

    def _exclusion_for(self, text: str, diagnosis: str, *, item: bool) -> dict[str, Any] | None:
        for ex in self.rules["exclusions"]:
            if ex.get("item_only") and not item:
                continue
            if first_match(text, ex["keywords"]):
                if first_match(diagnosis, ex.get("unless_diagnosis_mentions", [])):
                    continue
                return ex
        return None

    def _waiting_days(self, dotted: str) -> int:
        node: Any = self.policy["waiting_periods"]
        for part in dotted.split("."):
            node = node[part]
        return int(node)

    def is_network(self, provider: str | None) -> str | None:
        p = norm(provider)
        if not p:
            return None
        for name in self.policy.get("network_hospitals", []):
            n = norm(name)
            if n in p or has_phrase(p, n.split()[0]):
                return name
        return None

    # --------------------------------------------------------------- main entry
    def adjudicate(self, facts: ClaimFacts, *, claim_id: str, reviewer_override: bool = False) -> dict[str, Any]:
        ctx = _Ctx()
        form, docs = facts.form, facts.documents
        rx_docs = [d for d in docs if d.document_type in PRESCRIPTION_TYPES]
        bill_docs = [d for d in docs if d.document_type in BILL_TYPES]
        diagnosis = "; ".join(dict.fromkeys(d.diagnosis for d in rx_docs + docs if d.diagnosis))
        treatments = [t for d in rx_docs for t in d.treatments] or [t for d in docs for t in d.treatments]
        tests = [t for d in docs for t in d.tests]
        clinical_text = " ; ".join([diagnosis, *treatments])

        self._step1_eligibility(ctx, facts, clinical_text)
        self._step2_documents(ctx, facts, rx_docs, bill_docs)
        items = self._collect_items(facts, bill_docs)
        primary = self._primary_category(items, rx_docs, clinical_text)
        for it in items:
            if primary in self.rules["specialty_categories"] and it.category in (None, "consultation"):
                it.category = primary
        payable, rejected_items, excluded_total = self._step3_coverage(ctx, facts, items, diagnosis, treatments, tests)
        amounts = self._step4_limits(ctx, facts, payable, primary, bill_docs, excluded_total)
        self._step5_medical_and_risk(ctx, facts, bill_docs)

        # ----------------------------------------------------------- decision
        confidence = self._confidence(facts, ctx)
        reject_codes = [c for c, _ in ctx.reject]
        low_conf = facts.extraction_confidence < self.risk["minimum_confidence"]
        if reject_codes and low_conf and not reviewer_override and set(reject_codes) <= EVIDENCE_DEPENDENT_CODES:
            ctx.escalate("Decision", "Extraction confidence", "LOW_CONFIDENCE",
                         f"The rejection would rely on low-confidence extraction ({facts.extraction_confidence:.0%}); "
                         "a reviewer should confirm the documents.")
            reject_codes = []
        elif low_conf and not reject_codes:
            ctx.escalate("Decision", "Extraction confidence", "LOW_CONFIDENCE",
                         f"Extraction confidence {facts.extraction_confidence:.0%} is below the "
                         f"{self.risk['minimum_confidence']:.0%} automation threshold.")

        approved = amounts["approved"]
        if reject_codes:
            decision, approved = "REJECTED", 0.0
            reasons = [r for _, r in ctx.reject]
            notes = reasons[0]
            next_steps = NEXT_STEPS.get(reject_codes[0], "Review the reasons listed and resubmit with corrected documents.")
        elif ctx.review and not reviewer_override:
            decision, approved = "MANUAL_REVIEW", 0.0
            reasons = [r for _, r in ctx.review]
            notes = "Escalated for human review: " + "; ".join(r for _, r in ctx.review[:2])
            next_steps = "A claims reviewer will verify the original documents before any payment."
        elif ctx.partial:
            decision = "PARTIAL"
            reasons = [r for _, r in ctx.partial]
            notes = f"{inr(approved)} payable after applying policy exclusions/limits."
            next_steps = "The payable portion will be reimbursed; excluded or over-limit amounts are the member's responsibility."
        else:
            decision = "APPROVED"
            reasons = []
            notes = "All policy checks passed." + (f" {amounts['deduction_note']}" if amounts["deduction_note"] else "")
            next_steps = "Reimbursement will be processed to the member's registered account."
        if decision in {"APPROVED", "PARTIAL"} and approved <= 0:
            decision, approved = "REJECTED", 0.0
            reject_codes = reject_codes or ["SERVICE_NOT_COVERED"]
            reasons = ["No payable amount remains after applying the policy."]
            notes = reasons[0]
            next_steps = NEXT_STEPS["SERVICE_NOT_COVERED"]

        network = amounts["network_provider"]
        paying = decision in {"APPROVED", "PARTIAL"}
        cashless = bool(paying and form.cashless_request and network
                        and approved <= self.policy["cashless_facilities"]["instant_approval_limit"])
        return {
            "claim_id": claim_id,
            "decision": decision,
            "approved_amount": round(approved, 2),
            "claimed_amount": round(form.claim_amount, 2),
            "deduction": round(max(0.0, form.claim_amount - approved), 2),
            "deductions": amounts["deductions"] if paying else {},
            "rejection_reasons": reject_codes,
            "review_reasons": [c for c, _ in ctx.review] if decision == "MANUAL_REVIEW" else [],
            "flags": [s.label for s in facts.risk_signals],
            "reasons": reasons,
            "confidence_score": confidence,
            "notes": notes,
            "next_steps": next_steps,
            "checks": ctx.checks,
            "rejected_items": rejected_items,
            "line_items": [{"description": i.description, "amount": i.amount, "category": i.category,
                            "source_document": i.source_document} for i in items],
            "primary_category": primary,
            "network_provider": network,
            "cashless_approved": cashless,
            "network_discount": amounts["deductions"].get("network_discount", 0.0) if paying else 0.0,
        }

    # ------------------------------------------------------------------- steps
    def _step1_eligibility(self, ctx: _Ctx, facts: ClaimFacts, clinical_text: str) -> None:
        s = "1. Eligibility"
        form, member = facts.form, facts.member
        if member is None:
            ctx.fail(s, "Member verification", "MEMBER_NOT_COVERED",
                     f"Member ID {form.member_id} was not found in the policy member registry.")
            return
        if not names_match(form.member_name, member.member_name):
            ctx.fail(s, "Member verification", "PATIENT_MISMATCH",
                     f"Name on the claim form ('{form.member_name}') does not match policy records for {member.member_id}.")
        else:
            ctx.check(s, "Member verification", "pass", f"{member.member_id} ({member.member_name}) is a covered member.")

        effective = date.fromisoformat(self.policy["effective_date"])
        if not member.active or member.policy_id != self.policy["policy_id"] or form.treatment_date < effective:
            ctx.fail(s, "Policy status", "POLICY_INACTIVE",
                     f"Policy {self.policy['policy_id']} was not active for this member on {form.treatment_date.isoformat()}.")
        else:
            ctx.check(s, "Policy status", "pass", f"Policy {self.policy['policy_id']} active on {form.treatment_date.isoformat()}.")

        days = (form.treatment_date - member.join_date).days
        initial = int(self.policy["waiting_periods"]["initial_waiting"])
        problems = []
        if days < initial:
            eligible = member.join_date + timedelta(days=initial)
            problems.append(f"Initial {initial}-day waiting period applies. Eligible from {eligible.isoformat()}")
        for cond in self.rules["waiting_period_conditions"].values():
            if first_match(clinical_text, cond["keywords"]):
                period = self._waiting_days(cond["policy_key"])
                if days < period:
                    eligible = member.join_date + timedelta(days=period)
                    problems.append(f"{cond['label']} has {period}-day waiting period. Eligible from {eligible.isoformat()}")
        for pre in member.pre_existing_conditions:
            if has_phrase(clinical_text, pre):
                period = int(self.policy["waiting_periods"]["pre_existing_diseases"])
                if days < period:
                    eligible = member.join_date + timedelta(days=period)
                    problems.append(f"Pre-existing {pre} has {period}-day waiting period. Eligible from {eligible.isoformat()}")
        if problems:
            ctx.fail(s, "Waiting period", "WAITING_PERIOD", "; ".join(problems))
        else:
            ctx.check(s, "Waiting period", "pass",
                      f"Member joined {member.join_date.isoformat()} ({days} days before treatment); no waiting period applies.")

    def _step2_documents(self, ctx: _Ctx, facts: ClaimFacts, rx_docs: list[DocumentEvidence],
                         bill_docs: list[DocumentEvidence]) -> None:
        s = "2. Documents"
        form = facts.form
        missing = []
        if not rx_docs:
            missing.append("Prescription from registered doctor is required")
        if not bill_docs:
            missing.append("Original bill/receipt is required")
        if missing:
            ctx.fail(s, "Required documents", "MISSING_DOCUMENTS", "; ".join(missing))
        else:
            ctx.check(s, "Required documents", "pass", f"{len(rx_docs)} prescription(s) and {len(bill_docs)} bill(s) submitted.")

        relevant = rx_docs + bill_docs
        if relevant:
            worst = min(relevant, key=lambda d: d.legibility)
            if worst.legibility < self.risk["illegible_below"]:
                ctx.fail(s, "Legibility", "ILLEGIBLE_DOCUMENTS", f"{worst.file_name} could not be read reliably.")
            elif worst.legibility < self.risk["low_legibility_below"]:
                ctx.check(s, "Legibility", "warn", f"{worst.file_name} is only partly legible; extracted values carry lower confidence.")
                ctx.uncertainty.append("partly legible document")
            else:
                ctx.check(s, "Legibility", "pass", "Documents were readable.")

        if rx_docs:
            regs = [d.doctor_registration for d in rx_docs if d.doctor_registration]
            if not regs:
                ctx.fail(s, "Doctor registration", "DOCTOR_REG_INVALID",
                         "Doctor's registration number is not visible on the prescription.")
            elif not any(doctor_registration_valid(r, form.treatment_date) for r in regs):
                ctx.fail(s, "Doctor registration", "DOCTOR_REG_INVALID",
                         f"Registration '{regs[0]}' is not in the State/Number/Year format.")
            else:
                ctx.check(s, "Doctor registration", "pass", f"Registration {regs[0]} is well-formed.")

        if form.treatment_date > facts.submission_date:
            ctx.fail(s, "Date consistency", "DATE_MISMATCH", "Treatment date on the claim is in the future.")
        else:
            dated = [d for d in relevant if d.document_date]
            wrong = [d for d in dated if d.document_date != form.treatment_date]
            if wrong:
                detail = ", ".join(f"{d.file_name} is dated {d.document_date.isoformat()}" for d in wrong)
                ctx.fail(s, "Date consistency", "DATE_MISMATCH",
                         f"Treatment date on the claim is {form.treatment_date.isoformat()} but {detail}.")
            elif relevant and not dated:
                ctx.check(s, "Date consistency", "warn", "No visit/bill date could be read from the documents.")
                ctx.uncertainty.append("no document date")
            elif dated:
                ctx.check(s, "Date consistency", "pass",
                          f"Visit/bill dates match the treatment date {form.treatment_date.isoformat()} (follow-up dates are not compared).")

        reference = facts.member.member_name if facts.member else form.member_name
        named = [d for d in relevant if d.patient_name]
        mismatched = [d for d in named if not names_match(d.patient_name, reference)]
        if mismatched:
            ctx.fail(s, "Patient details", "PATIENT_MISMATCH",
                     f"{mismatched[0].file_name} is issued to '{mismatched[0].patient_name}', not {reference}.")
        elif named:
            ctx.check(s, "Patient details", "pass", f"Patient name on the documents matches {reference}.")
        elif relevant:
            ctx.check(s, "Patient details", "warn", "Patient name was not found on the documents.")
            ctx.uncertainty.append("no patient name")

    def _collect_items(self, facts: ClaimFacts, bill_docs: list[DocumentEvidence]) -> list[LineItem]:
        items: list[LineItem] = []
        for d in bill_docs:
            if d.line_items:
                for it in d.line_items:
                    if not it.amount or it.amount <= 0:
                        continue
                    cat = (self.categorise(it.description)
                           or facts.medical.item_categories.get(it.description)
                           or it.category)
                    items.append(LineItem(it.description, round(float(it.amount), 2), cat, d.file_name))
            elif d.total_amount:
                cat = "pharmacy" if d.document_type == "pharmacy_bill" else None
                items.append(LineItem("Billed amount (not itemised)", d.total_amount, cat, d.file_name))
        return items

    def _primary_category(self, items: list[LineItem], rx_docs: list[DocumentEvidence], clinical_text: str) -> str:
        cats = {i.category for i in items}
        regs = " ".join(d.doctor_registration or "" for d in rx_docs).upper()
        who = " ".join(f"{d.doctor_name or ''} {d.provider_name or ''}" for d in rx_docs)
        kw = self.rules["category_keywords"]
        for spec in self.rules["specialty_categories"]:
            if spec in cats or first_match(clinical_text, kw[spec]):
                return spec
        if any(f" {p}/" in f" {regs}" for p in self.rules["alternative_registration_prefixes"]) \
                or first_match(who, kw["alternative"]):
            return "alternative"
        if items and cats == {"diagnostic"}:
            return "diagnostic"
        if items and cats == {"pharmacy"}:
            return "pharmacy"
        return "consultation"

    def _step3_coverage(self, ctx: _Ctx, facts: ClaimFacts, items: list[LineItem], diagnosis: str,
                        treatments: list[str], tests: list[str]) -> tuple[list[LineItem], list[str], float]:
        s = "3. Coverage"
        claim_level: dict[str, Any] | None = None
        for ex in self.rules["exclusions"]:
            if ex.get("type") == "condition" and first_match(diagnosis, ex["keywords"]):
                claim_level = ex
                ctx.fail(s, "Exclusions", "EXCLUDED_CONDITION", f"{ex['name']} is excluded from coverage.")
                break
        if claim_level is None:
            entries = treatments or ([diagnosis] if diagnosis else [])
            matched = [self._exclusion_for(t, diagnosis, item=False) for t in entries]
            if entries and all(matched):
                claim_level = matched[0]
                ctx.fail(s, "Exclusions", "SERVICE_NOT_COVERED", f"{claim_level['name']} are excluded from coverage.")

        payable: list[LineItem] = []
        rejected: list[str] = []
        excluded_total = 0.0
        for it in items:
            ex = claim_level or self._exclusion_for(it.description, diagnosis, item=True)
            if ex:
                rejected.append(f"{it.description} - {ex['label']}")
                excluded_total += it.amount
            else:
                payable.append(it)
        if claim_level is None:
            if rejected and not payable:
                ctx.fail(s, "Exclusions", "SERVICE_NOT_COVERED", "Every billed item falls under a policy exclusion.")
            elif rejected:
                ctx.check(s, "Exclusions", "warn", f"Not covered: {', '.join(rejected)} ({inr(excluded_total)}).", "EXCLUDED_ITEMS")
                ctx.partial.append(("EXCLUDED_ITEMS", f"Not covered by the policy: {', '.join(rejected)}."))
            else:
                ctx.check(s, "Exclusions", "pass", "No policy exclusion applies to the diagnosis, treatment or billed items.")

        pa = self.rules["pre_authorization"]
        needing = []
        for proc in pa["procedures"]:
            billed = sum(i.amount for i in items if first_match(i.description, proc["keywords"]))
            prescribed = any(first_match(t, proc["keywords"]) for t in tests)
            if billed > pa["amount_threshold"] or (billed == 0 and prescribed and facts.form.claim_amount > pa["amount_threshold"]):
                needing.append(proc["name"])
        if needing and not facts.preauth_valid:
            ctx.fail(s, "Pre-authorization", "PRE_AUTH_MISSING",
                     f"{'/'.join(needing)} requires pre-authorization for claims above {inr(pa['amount_threshold'])}; none was found.")
        elif needing:
            ctx.check(s, "Pre-authorization", "pass", f"Valid pre-authorization found for {'/'.join(needing)}.")
        else:
            ctx.check(s, "Pre-authorization", "pass", "No procedure on this claim needs pre-authorization.")
        return payable, rejected, round(excluded_total, 2)

    def _step4_limits(self, ctx: _Ctx, facts: ClaimFacts, payable: list[LineItem], primary: str,
                      bill_docs: list[DocumentEvidence], excluded_total: float) -> dict[str, Any]:
        s = "4. Limits"
        form = facts.form
        req = self.policy["claim_requirements"]
        deductions: dict[str, float] = {}
        if excluded_total:
            deductions["excluded_items"] = excluded_total
        if form.claim_amount < req["minimum_claim_amount"]:
            ctx.fail(s, "Minimum amount", "BELOW_MIN_AMOUNT",
                     f"Claim of {inr(form.claim_amount)} is below the {inr(req['minimum_claim_amount'])} minimum.")
        late_by = (facts.submission_date - form.treatment_date).days
        if late_by > req["submission_timeline_days"]:
            ctx.fail(s, "Submission window", "LATE_SUBMISSION",
                     f"Submitted {late_by} days after treatment; the limit is {req['submission_timeline_days']} days.")
        else:
            ctx.check(s, "Submission window", "pass", f"Submitted {max(late_by, 0)} day(s) after treatment.")

        covered = round(sum(i.amount for i in payable), 2)
        base = min(covered, form.claim_amount)

        by_cat: dict[str, float] = {}
        for it in payable:
            by_cat[it.category or "other"] = by_cat.get(it.category or "other", 0.0) + it.amount
        over = []
        for cat, total in by_cat.items():
            key = self.rules["sub_limit_keys"].get(cat)
            if key and total > float(self.cov[key]["sub_limit"]):
                over.append((cat, total, float(self.cov[key]["sub_limit"])))
        if over:
            cut = round(sum(t - lim for _, t, lim in over), 2)
            base = max(0.0, round(base - cut, 2))
            deductions["sub_limit"] = cut
            desc = ", ".join(f"{c} {inr(t)} > {inr(lim)}" for c, t, lim in over)
            ctx.check(s, "Category sub-limits", "warn", f"Capped at the sub-limit: {desc}.", "SUB_LIMIT_EXCEEDED")
            ctx.partial.append(("SUB_LIMIT_EXCEEDED", f"Amount above the category sub-limit is not payable ({desc})."))
        else:
            ctx.check(s, "Category sub-limits", "pass", "All categories are within their sub-limits.")

        per_claim = float(self.cov["per_claim_limit"])
        if primary in self.rules["per_claim_limit_applies_to"] and base > per_claim:
            ctx.fail(s, "Per-claim limit", "PER_CLAIM_EXCEEDED", f"Claim amount exceeds per-claim limit of {inr(per_claim)}")
        elif primary in self.rules["per_claim_limit_applies_to"]:
            ctx.check(s, "Per-claim limit", "pass", f"{inr(base)} is within the {inr(per_claim)} per-claim limit.")
        else:
            ctx.check(s, "Per-claim limit", "info",
                      f"{primary.title()} claims are capped by their category sub-limit rather than the general per-claim limit.")

        network = next((n for n in (self.is_network(d.provider_name) for d in bill_docs) if n), None)
        if form.hospital and self.is_network(form.hospital) and not network:
            ctx.check(s, "Network provider", "warn",
                      f"The claim form names {form.hospital}, but no bill shows a network provider, so network terms were not applied.")
            ctx.uncertainty.append("network provider unconfirmed")
        note = ""
        consult = self.cov["consultation_fees"]
        if network and base > 0:
            disc = round(base * float(consult["network_discount"]) / 100, 2)
            base = round(base - disc, 2)
            deductions["network_discount"] = disc
            note = f"{consult['network_discount']}% network discount ({inr(disc)}) applied at {network}."
            ctx.check(s, "Network / co-pay", "pass", note + " Network claims carry no member co-pay.")
        elif primary in self.rules["copay_applies_to"] and base > 0:
            cop = round(base * float(consult["copay_percentage"]) / 100, 2)
            base = round(base - cop, 2)
            deductions["copay"] = cop
            note = f"{consult['copay_percentage']}% consultation co-pay ({inr(cop)}) applied."
            ctx.check(s, "Network / co-pay", "pass", note)
        else:
            ctx.check(s, "Network / co-pay", "pass", f"No co-pay applies to {primary} claims.")

        annual = float(self.cov["annual_limit"])
        remaining = max(0.0, annual - facts.history.ytd_paid)
        if base > 0 and remaining <= 0:
            ctx.fail(s, "Annual limit", "ANNUAL_LIMIT_EXCEEDED",
                     f"Annual limit of {inr(annual)} is already used ({inr(facts.history.ytd_paid)} paid this year).")
        elif base > remaining:
            deductions["annual_limit"] = round(base - remaining, 2)
            base = remaining
            ctx.check(s, "Annual limit", "warn", f"Only {inr(remaining)} of the {inr(annual)} annual limit remains.", "ANNUAL_LIMIT_EXCEEDED")
            ctx.partial.append(("ANNUAL_LIMIT_EXCEEDED", f"Payment capped at the remaining annual limit ({inr(remaining)})."))
        else:
            ctx.check(s, "Annual limit", "pass", f"{inr(facts.history.ytd_paid)} of {inr(annual)} used this policy year.")
        return {"approved": round(base, 2), "deductions": deductions, "deduction_note": note, "network_provider": network}

    def _step5_medical_and_risk(self, ctx: _Ctx, facts: ClaimFacts, bill_docs: list[DocumentEvidence]) -> None:
        s = "5. Medical & risk"
        med = facts.medical
        if med.available and med.consistent is False and med.confidence >= self.risk["medical_concern_min_confidence"]:
            ctx.escalate(s, "Medical necessity", "NOT_MEDICALLY_NECESSARY",
                         "Treatment may not be justified by the diagnosis: " + ("; ".join(med.concerns) or med.rationale))
        elif med.available:
            ctx.check(s, "Medical necessity", "pass", med.rationale or "Treatment is consistent with the diagnosis.")
        else:
            ctx.check(s, "Medical necessity", "info", "Medical review agent unavailable; no concern raised.")

        for sig in facts.risk_signals:
            if sig.severity == "reject":
                ctx.fail(s, sig.label, sig.code, sig.detail)
            else:
                ctx.escalate(s, sig.label, sig.code, sig.detail)
        if not facts.risk_signals:
            ctx.check(s, "Fraud & tampering", "pass", "No fraud, tampering or injected-instruction signals.")

        tol = self.risk["amount_tolerance_inr"]
        for d in bill_docs:
            if d.line_items and d.total_amount:
                s_items = round(sum(i.amount for i in d.line_items if i.amount), 2)
                if abs(s_items - d.total_amount) > tol:
                    ctx.escalate(s, "Bill arithmetic", "AMOUNT_MISMATCH",
                                 f"{d.file_name}: line items add up to {inr(s_items)} but the printed total is {inr(d.total_amount)}.")
        billed = round(sum((d.total_amount or sum(i.amount for i in d.line_items if i.amount)) for d in bill_docs), 2)
        if bill_docs and billed and abs(billed - facts.form.claim_amount) > tol:
            ctx.escalate(s, "Claimed vs billed", "AMOUNT_MISMATCH",
                         f"The claim form asks for {inr(facts.form.claim_amount)} but the bills total {inr(billed)}.")
        elif bill_docs and billed:
            ctx.check(s, "Claimed vs billed", "pass", f"Claimed amount matches the bills ({inr(billed)}).")
        if facts.form.claim_amount > self.risk["high_value_manual_review_above"]:
            ctx.escalate(s, "High-value claim", "HIGH_VALUE_MANUAL_REVIEW",
                         f"Claims above {inr(self.risk['high_value_manual_review_above'])} always get human review.")

    def _confidence(self, facts: ClaimFacts, ctx: _Ctx) -> float:
        """Confidence that the facts behind the decision were read correctly.

        Starts from the extraction agent's field-level confidence and drops for
        each unresolved evidence gap. It is deliberately *not* lowered because a
        policy rule failed: a clearly read excluded treatment is a confident
        rejection.
        """
        score = facts.extraction_confidence - 0.05 * len(set(ctx.uncertainty))
        return round(max(0.3, min(0.99, score)), 2)
