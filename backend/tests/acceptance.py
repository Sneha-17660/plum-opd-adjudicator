"""Adapter from the supplied test_cases.json to engine input.

The supplied cases describe *already-read* documents, so this adapter plays
the role of the OCR/extraction agents: it turns ``input_data`` into
``DocumentEvidence`` objects. It never reads ``expected_output``. Member
records come from the real registry, and risk signals are produced by the real
RiskAgent from the case's claim history.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.agents.support_agents import RiskAgent, load_members
from app.engine.policy_engine import PolicyEngine
from app.models import ClaimFacts, ClaimForm, DocumentEvidence, HistoryContext, LineItem, MedicalAssessment

CASES_FILE = Path(__file__).resolve().parents[1] / "data" / "test_cases.json"


def load_cases() -> list[dict[str, Any]]:
    return json.loads(CASES_FILE.read_text(encoding="utf-8"))["test_cases"]


def case_to_facts(case: dict[str, Any], engine: PolicyEngine) -> ClaimFacts:
    data = case["input_data"]
    t = date.fromisoformat(data["treatment_date"])
    docs_in = data.get("documents", {})
    docs: list[DocumentEvidence] = []
    rx = docs_in.get("prescription")
    if rx:
        docs.append(DocumentEvidence(
            file_name="prescription", sha256=f"{case['case_id']}-rx", mime="application/pdf",
            document_type="prescription", patient_name=data["member_name"], doctor_name=rx.get("doctor_name"),
            doctor_registration=rx.get("doctor_reg"), document_date=t, diagnosis=rx.get("diagnosis"),
            treatments=[x for x in [rx.get("treatment"), *rx.get("procedures", [])] if x],
            medicines=list(rx.get("medicines_prescribed", [])), tests=list(rx.get("tests_prescribed", [])),
            field_confidence={"patient_name": 0.95, "document_date": 0.95, "doctor_registration": 0.95, "diagnosis": 0.95},
        ))
    bill = docs_in.get("bill")
    if bill:
        items = [LineItem(k.replace("_", " ").capitalize(), float(v)) for k, v in bill.items() if isinstance(v, (int, float))]
        docs.append(DocumentEvidence(
            file_name="bill", sha256=f"{case['case_id']}-bill", mime="application/pdf", document_type="medical_bill",
            patient_name=data["member_name"], provider_name=data.get("hospital"), document_date=t,
            tests=list(bill.get("test_names", [])), line_items=items, total_amount=sum(i.amount for i in items),
            field_confidence={"patient_name": 0.95, "document_date": 0.95, "line_items": 0.95, "total_amount": 0.95},
        ))
    history = HistoryContext(previous_claims_same_day=int(data.get("previous_claims_same_day", 0)),
                             claims_last_30_days=int(data.get("previous_claims_same_day", 0)))
    member = load_members().get(data["member_id"])
    return ClaimFacts(
        form=ClaimForm(data["member_id"], data["member_name"], t, float(data["claim_amount"]),
                       data.get("hospital"), bool(data.get("cashless_request"))),
        member=member, documents=docs, history=history, medical=MedicalAssessment(),
        risk_signals=RiskAgent(engine.rules).assess(docs, history), preauth_valid=False,
        submission_date=t + timedelta(days=5), extraction_confidence=0.95,
    )


def run_case(case: dict[str, Any], engine: PolicyEngine | None = None) -> dict[str, Any]:
    engine = engine or PolicyEngine()
    return engine.adjudicate(case_to_facts(case, engine), claim_id=f"TEST_{case['case_id']}")


def compare(case: dict[str, Any], result: dict[str, Any]) -> list[str]:
    """Differences between the engine result and the supplied expectation."""
    exp, problems = case["expected_output"], []
    if result["decision"] != exp["decision"]:
        problems.append(f"decision {result['decision']} != {exp['decision']}")
    if "approved_amount" in exp and abs(result["approved_amount"] - exp["approved_amount"]) > 0.01:
        problems.append(f"approved {result['approved_amount']} != {exp['approved_amount']}")
    for code in exp.get("rejection_reasons", []):
        if code not in result["rejection_reasons"]:
            problems.append(f"missing reason {code} (got {result['rejection_reasons']})")
    if "rejected_items" in exp and result["rejected_items"] != exp["rejected_items"]:
        problems.append(f"rejected_items {result['rejected_items']} != {exp['rejected_items']}")
    for key in ("cashless_approved", "network_discount"):
        if key in exp and result[key] != exp[key]:
            problems.append(f"{key} {result[key]} != {exp[key]}")
    return problems
