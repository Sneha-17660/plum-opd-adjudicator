"""Internal data model shared by the agents and the policy engine."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from typing import Any, Literal

DocumentType = Literal[
    "prescription", "medical_bill", "pharmacy_bill", "diagnostic_report",
    "pre_authorization", "discharge_summary", "other",
]
PRESCRIPTION_TYPES = {"prescription"}
BILL_TYPES = {"medical_bill", "pharmacy_bill"}


@dataclass
class LineItem:
    description: str
    amount: float
    category: str | None = None
    source_document: str = ""


@dataclass
class DocumentEvidence:
    """Facts read from one uploaded document (by the OCR + extraction agents)."""
    file_name: str
    sha256: str
    mime: str
    document_type: str = "other"
    patient_name: str | None = None
    patient_age: str | None = None
    patient_gender: str | None = None
    provider_name: str | None = None
    doctor_name: str | None = None
    doctor_registration: str | None = None
    document_date: date | None = None
    follow_up_date: date | None = None
    diagnosis: str | None = None
    treatments: list[str] = field(default_factory=list)
    medicines: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    line_items: list[LineItem] = field(default_factory=list)
    total_amount: float | None = None
    invoice_number: str | None = None
    pre_auth_reference: str | None = None
    field_confidence: dict[str, float] = field(default_factory=dict)
    legibility: float = 1.0
    handwritten: bool = False
    visual_flags: list[str] = field(default_factory=list)
    injection_text: list[str] = field(default_factory=list)
    transcript: str = ""
    text_layer_chars: int = 0
    extraction_warnings: list[str] = field(default_factory=list)


@dataclass
class ClaimForm:
    """What the claimant states when submitting (the trusted-ish envelope)."""
    member_id: str
    member_name: str
    treatment_date: date
    claim_amount: float
    hospital: str | None = None
    cashless_request: bool = False


@dataclass
class MemberRecord:
    member_id: str
    member_name: str
    policy_id: str
    active: bool
    join_date: date
    pre_existing_conditions: list[str] = field(default_factory=list)


@dataclass
class HistoryContext:
    ytd_paid: float = 0.0
    previous_claims_same_day: int = 0
    claims_last_30_days: int = 0
    duplicate_of: list[str] = field(default_factory=list)


@dataclass
class MedicalAssessment:
    consistent: bool | None = None
    confidence: float = 0.0
    concerns: list[str] = field(default_factory=list)
    rationale: str = ""
    item_categories: dict[str, str] = field(default_factory=dict)
    available: bool = False


@dataclass
class RiskSignal:
    code: str
    label: str
    severity: Literal["review", "reject"]
    detail: str


@dataclass
class ClaimFacts:
    form: ClaimForm
    member: MemberRecord | None
    documents: list[DocumentEvidence]
    history: HistoryContext
    medical: MedicalAssessment
    risk_signals: list[RiskSignal]
    preauth_valid: bool
    submission_date: date
    extraction_confidence: float


def to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses/dates into JSON-safe structures."""
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _d(value: Any) -> date | None:
    return date.fromisoformat(value) if value else None


def facts_from_dict(data: dict[str, Any]) -> ClaimFacts:
    """Inverse of ``to_jsonable(ClaimFacts)`` — used to re-run the engine on review."""
    f = data["form"]
    form = ClaimForm(f["member_id"], f["member_name"], date.fromisoformat(f["treatment_date"]), float(f["claim_amount"]),
                     f.get("hospital"), bool(f.get("cashless_request")))
    m = data.get("member")
    member = MemberRecord(m["member_id"], m["member_name"], m["policy_id"], m["active"], date.fromisoformat(m["join_date"]),
                          list(m.get("pre_existing_conditions", []))) if m else None
    docs = []
    for d in data.get("documents", []):
        d = dict(d)
        d["document_date"], d["follow_up_date"] = _d(d.get("document_date")), _d(d.get("follow_up_date"))
        d["line_items"] = [LineItem(**li) for li in d.get("line_items", [])]
        docs.append(DocumentEvidence(**d))
    return ClaimFacts(
        form=form, member=member, documents=docs,
        history=HistoryContext(**data.get("history", {})),
        medical=MedicalAssessment(**data.get("medical", {})),
        risk_signals=[RiskSignal(**r) for r in data.get("risk_signals", [])],
        preauth_valid=bool(data.get("preauth_valid")),
        submission_date=date.fromisoformat(data["submission_date"]),
        extraction_confidence=float(data.get("extraction_confidence", 0.0)),
    )
