"""End-to-end API tests: sample documents -> agents (fake LLM) -> engine -> store."""
from __future__ import annotations

import re
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

from app import main, samples
from app.config import get_settings
from app.llm.groq_client import GroqClient
from app.services.pipeline import ClaimPipeline
from tests import fake_groq

EXPECTED = {
    "consultation": ("APPROVED", 1350, []),
    "dental": ("PARTIAL", 8000, []),
    "per-claim-limit": ("REJECTED", 0, ["PER_CLAIM_EXCEEDED"]),
    "missing-prescription": ("REJECTED", 0, ["MISSING_DOCUMENTS"]),
    "ayurveda": ("APPROVED", 4000, []),
    "mri-no-preauth": ("REJECTED", 0, ["PRE_AUTH_MISSING"]),
    "same-day-claims": ("MANUAL_REVIEW", 0, ["MULTIPLE_CLAIMS_SAME_DAY"]),
    "weight-loss": ("REJECTED", 0, ["SERVICE_NOT_COVERED"]),
    "network-cashless": ("APPROVED", 3600, []),
    "prompt-injection": ("MANUAL_REVIEW", 0, ["PROMPT_INJECTION"]),
    "amount-mismatch": ("MANUAL_REVIEW", 0, ["AMOUNT_MISMATCH"]),
    "patient-mismatch": ("REJECTED", 0, ["PATIENT_MISMATCH"]),
}
TOKEN = "test-reviewer-token"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)
    monkeypatch.setenv("RATE_LIMIT_PER_10_MIN", "1000")
    with TestClient(main.app) as c:
        s = get_settings()
        main.state.pipeline = ClaimPipeline(s, main.state.store, main.state.engine,
                                            GroqClient(s, transport=fake_groq.transport))
        yield c


def _files_for(sample_id: str) -> tuple[dict, list]:
    sample = samples.BY_ID[sample_id]
    form = samples.describe(sample)["form"]
    files = samples.build_files(sample)
    for name, mime, content in files:
        if mime == "application/pdf":
            text = fitz.open(stream=content, filetype="pdf")[0].get_text()
        else:  # the image prescription: the fake "vision model" reads the source layout
            text = samples._prescription(sample.rx, samples.treatment_date()).load_page(0).get_text()
        fake_groq.TRANSCRIPTS[name] = text
    data = {k: (str(v).lower() if isinstance(v, bool) else str(v)) for k, v in form.items()}
    return data, [("files", (n, c, m)) for n, m, c in files]


def _submit(client, sample_id: str):
    data, files = _files_for(sample_id)
    return client.post("/api/claims", data=data, files=files)


@pytest.mark.parametrize("sample_id", list(EXPECTED))
def test_every_sample_through_the_live_route(client, sample_id):
    r = _submit(client, sample_id)
    assert r.status_code == 200, r.text
    body = r.json()
    decision, amount, codes = EXPECTED[sample_id]
    assert body["decision"] == decision, (body["reasons"], body["checks"])
    assert body["approved_amount"] == amount
    for code in codes:
        assert code in body["rejection_reasons"] + body["review_reasons"]
    assert [t["agent"] for t in body["trace"]] == [
        "Intake Agent", "OCR Agent", "Extraction Agent", "Eligibility Agent",
        "Medical Review Agent", "Fraud & Risk Agent", "Policy Engine"]
    assert body["final_authority"] == "Policy Engine (deterministic)"
    assert client.get(f"/api/claims/{body['claim_id']}").json()["decision"] == decision


def test_follow_up_date_on_prescription_does_not_reject(client):
    body = _submit(client, "consultation").json()
    rx = next(d for d in body["documents"] if d["document_type"] == "prescription")
    assert rx["follow_up_date"] and rx["follow_up_date"] != rx["document_date"]
    assert body["decision"] == "APPROVED"


def test_cashless_and_network_discount(client):
    body = _submit(client, "network-cashless").json()
    assert body["cashless_approved"] is True and body["network_discount"] == 900


def test_resubmitting_the_same_bill_is_a_duplicate(client):
    data, files = _files_for("consultation")
    first = client.post("/api/claims", data=data, files=files).json()
    second = client.post("/api/claims", data=data, files=files).json()
    assert first["decision"] == "APPROVED"
    assert second["decision"] == "REJECTED" and "DUPLICATE_CLAIM" in second["rejection_reasons"]


def test_unknown_member_is_rejected(client):
    data, files = _files_for("consultation")
    data["member_id"] = "EMP999"
    body = client.post("/api/claims", data=data, files=files).json()
    assert body["decision"] == "REJECTED" and body["rejection_reasons"][0] == "MEMBER_NOT_COVERED"


def test_upload_requires_claim_form_fields(client):
    _, files = _files_for("consultation")
    assert client.post("/api/claims", files=files).status_code == 422


def test_rejects_non_documents(client):
    data, _ = _files_for("consultation")
    r = client.post("/api/claims", data=data, files=[("files", ("x.txt", b"hello", "text/plain"))])
    assert r.status_code == 400 and "not a PDF" in r.json()["detail"]


def test_reviewer_needs_token_and_amount_is_recomputed(client):
    claim = _submit(client, "prompt-injection").json()
    url = f"/api/claims/{claim['claim_id']}/review"
    assert client.post(url, json={"action": "APPROVE"}).status_code == 401
    assert client.post(url, json={"action": "APPROVE"}, headers={"X-Reviewer-Token": "wrong"}).status_code == 401
    r = client.post(url, json={"action": "APPROVE", "note": "Instruction is a printing artefact"},
                    headers={"X-Reviewer-Token": TOKEN})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "APPROVED"
    assert body["approved_amount"] == 1125          # 1,250 less 10% co-pay, computed by the engine
    assert body["reviews"][0]["action"] == "APPROVE"
    assert client.post(url, json={"action": "APPROVE"}, headers={"X-Reviewer-Token": TOKEN}).status_code == 409


def test_reviewer_cannot_approve_what_policy_rejects(client, monkeypatch):
    # amount-mismatch -> manual review; approval pays the evidenced amount, not the claimed one
    claim = _submit(client, "amount-mismatch").json()
    r = client.post(f"/api/claims/{claim['claim_id']}/review", json={"action": "APPROVE"},
                    headers={"X-Reviewer-Token": TOKEN})
    assert r.status_code == 200
    assert r.json()["approved_amount"] == 900       # items total 1,000 less co-pay; not the 1,800 claimed


def test_reviewer_actions_disabled_without_server_token(client, monkeypatch):
    claim = _submit(client, "prompt-injection").json()
    monkeypatch.setenv("REVIEWER_TOKEN", "")
    r = client.post(f"/api/claims/{claim['claim_id']}/review", json={"action": "APPROVE"},
                    headers={"X-Reviewer-Token": "anything"})
    assert r.status_code == 503


def test_reject_requires_note(client):
    claim = _submit(client, "same-day-claims").json()
    url = f"/api/claims/{claim['claim_id']}/review"
    assert client.post(url, json={"action": "REJECT"}, headers={"X-Reviewer-Token": TOKEN}).status_code == 400
    r = client.post(url, json={"action": "REJECT", "note": "Confirmed duplicate visits"}, headers={"X-Reviewer-Token": TOKEN})
    assert r.json()["decision"] == "REJECTED"


def test_public_read_routes(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/api/policy").json()["policy_id"] == "PLUM_OPD_2024"
    assert len(client.get("/api/members").json()["members"]) >= 10
    listed = client.get("/api/samples").json()["samples"]
    assert {s["id"] for s in listed} == set(EXPECTED)
    r = client.get("/api/samples/dental/files/1")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    stats = client.get("/api/stats").json()
    assert stats["total"] == 0
    assert client.get("/api/claims").json() == {"claims": []}


def test_frontend_only_calls_routes_that_exist():
    """Guards against the frontend drifting from the backend."""
    src = (Path(__file__).resolve().parents[2] / "frontend" / "app" / "page.tsx").read_text(encoding="utf-8")
    called = set(re.findall(r"`?\$\{API\}(/[a-z/\-]+)", src)) | set(re.findall(r"api\(['`](/[a-z/\-]+)", src))
    assert called, "no API calls found in the frontend"
    routes = {r.path for r in main.app.routes}
    for path in called:
        pattern = re.sub(r"\{[^}]+\}", "[^/]+", path)
        assert any(re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", route), path.rstrip("/")) or
                   re.fullmatch(pattern, route) for route in routes), f"frontend calls missing route {path}"
