"""Policy engine: the supplied acceptance suite plus targeted rule tests.

The acceptance adapter (tests/acceptance.py) converts each case's input into
document evidence; it never reads expected_output.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.agents.support_agents import RiskAgent, load_members
from app.engine.policy_engine import PolicyEngine
from app.models import ClaimFacts, ClaimForm, DocumentEvidence, HistoryContext, LineItem, MedicalAssessment
from tests.acceptance import compare, load_cases, run_case

ENGINE = PolicyEngine()
T = date(2024, 11, 1)


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["case_id"])
def test_supplied_acceptance_case(case):
    result = run_case(case, ENGINE)
    assert compare(case, result) == []


def test_waiting_period_note_names_the_eligibility_date():
    case = next(c for c in load_cases() if c["case_id"] == "TC005")
    result = run_case(case, ENGINE)
    assert "Diabetes has 90-day waiting period. Eligible from 2024-11-30" in result["notes"]


def facts(*, items=((("Consultation fee", 1000)),), diagnosis="Viral fever", reg="KA/45678/2015", amount=None,
          member="EMP001", name="Rajesh Kumar", rx_date=T, bill_date=T, total=None, legibility=0.95,
          medical=None, history=None, injection=(), treatments=(), tests=(), confidence=0.95) -> ClaimFacts:
    line_items = [LineItem(d, float(a)) for d, a in items]
    rx = DocumentEvidence("rx.pdf", "h-rx", "application/pdf", "prescription", patient_name=name,
                          doctor_registration=reg, document_date=rx_date, diagnosis=diagnosis,
                          treatments=list(treatments), tests=list(tests), legibility=legibility)
    bill = DocumentEvidence("bill.pdf", "h-bill", "application/pdf", "medical_bill", patient_name=name,
                            document_date=bill_date, line_items=line_items,
                            total_amount=total if total is not None else sum(i.amount for i in line_items),
                            legibility=legibility, injection_text=list(injection))
    history = history or HistoryContext()
    docs = [rx, bill]
    return ClaimFacts(ClaimForm(member, name, T, float(amount if amount is not None else sum(a for _, a in items))),
                      load_members().get(member), docs, history, medical or MedicalAssessment(),
                      RiskAgent(ENGINE.rules).assess(docs, history), False, T + timedelta(days=2), confidence)


def decide(f: ClaimFacts) -> dict:
    return ENGINE.adjudicate(f, claim_id="UNIT")


def test_clean_consultation_pays_after_copay():
    r = decide(facts())
    assert (r["decision"], r["approved_amount"], r["deductions"]["copay"]) == ("APPROVED", 900, 100)


def test_follow_up_date_is_not_a_date_mismatch():
    f = facts()
    f.documents[0].follow_up_date = T + timedelta(days=7)
    assert decide(f)["decision"] == "APPROVED"


def test_bill_dated_differently_is_rejected():
    r = decide(facts(bill_date=T + timedelta(days=1)))
    assert r["decision"] == "REJECTED" and "DATE_MISMATCH" in r["rejection_reasons"]


def test_missing_registration_is_rejected():
    r = decide(facts(reg=None))
    assert "DOCTOR_REG_INVALID" in r["rejection_reasons"]


def test_future_registration_year_is_invalid():
    assert "DOCTOR_REG_INVALID" in decide(facts(reg="KA/12345/2031"))["rejection_reasons"]


def test_unknown_member_is_rejected():
    assert decide(facts(member="EMP999"))["rejection_reasons"][0] == "MEMBER_NOT_COVERED"


def test_documents_in_another_name_are_rejected():
    f = facts()
    f.documents[1].patient_name = "Kiran Singh"
    assert "PATIENT_MISMATCH" in decide(f)["rejection_reasons"]


def test_below_minimum_amount_is_rejected():
    assert "BELOW_MIN_AMOUNT" in decide(facts(items=(("Consultation fee", 300),)))["rejection_reasons"]


def test_vitamins_excluded_unless_deficiency():
    items = (("Consultation fee", 1000), ("Vitamin D3 supplement", 400))
    r = decide(facts(items=items))
    assert r["decision"] == "PARTIAL" and r["approved_amount"] == 900
    assert decide(facts(items=items, diagnosis="Vitamin D deficiency"))["decision"] == "APPROVED"


def test_consultation_sub_limit_caps_payment():
    r = decide(facts(items=(("Consultation fee", 2500),)))
    assert r["decision"] == "PARTIAL" and r["approved_amount"] == 1800   # 2,000 cap less 10% co-pay


def test_annual_limit_caps_payment():
    r = decide(facts(history=HistoryContext(ytd_paid=49_500)))
    assert r["decision"] == "PARTIAL" and r["approved_amount"] == 500


def test_high_value_goes_to_manual_review():
    items = (("Root canal treatment", 9000), ("Dental crown", 1000), ("Consultation", 16000))
    r = decide(facts(items=items, diagnosis="Dental caries", amount=26000))
    assert r["decision"] in {"MANUAL_REVIEW", "REJECTED"}
    assert r["approved_amount"] == 0


def test_medical_concern_escalates_but_never_rejects():
    med = MedicalAssessment(consistent=False, confidence=0.9, concerns=["MRI for viral fever"], available=True)
    r = decide(facts(medical=med))
    assert r["decision"] == "MANUAL_REVIEW" and "NOT_MEDICALLY_NECESSARY" in r["review_reasons"]


def test_low_confidence_rejection_becomes_manual_review():
    r = decide(facts(reg=None, confidence=0.5))
    assert r["decision"] == "MANUAL_REVIEW" and "LOW_CONFIDENCE" in r["review_reasons"]


def test_confidence_is_not_lowered_by_a_clear_rejection():
    clean = decide(facts())["confidence_score"]
    rejected = decide(facts(items=(("Consultation fee", 300),)))["confidence_score"]
    assert rejected == clean


def test_confidence_drops_with_evidence_gaps():
    f = facts()
    f.documents[0].document_date = None
    f.documents[1].document_date = None
    assert decide(f)["confidence_score"] < decide(facts())["confidence_score"]
