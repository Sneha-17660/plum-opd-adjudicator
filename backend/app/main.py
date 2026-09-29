"""HTTP API for the OPD claim adjudicator.

Request flow for POST /api/claims:

    claim form + files
      -> ClaimPipeline (app/services/pipeline.py)
           Intake -> OCR (vision LLM) -> Extraction (text LLM)
           -> [Eligibility | Medical Review (LLM) | Fraud & Risk]
           -> PolicyEngine (deterministic, the only component that decides)
      -> ClaimStore (SQLite)

The LLM agents only produce evidence. The decision, amounts and reasons come
from app/engine/policy_engine.py.
"""
from __future__ import annotations

import logging
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any, Literal

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import samples
from app.agents.intake import IntakeError
from app.config import BUNDLED_DATA_DIR, get_settings
from app.engine.policy_engine import PolicyEngine
from app.llm.groq_client import LLMError
from app.models import ClaimForm, facts_from_dict
from app.services.pipeline import ClaimPipeline
from app.services.store import ClaimStore

log = logging.getLogger("api")
VERSION = "4.0.0"


class AppState:
    store: ClaimStore
    engine: PolicyEngine
    pipeline: ClaimPipeline


state = AppState()


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    state.store = ClaimStore(settings.runtime_dir / "claims.db")
    state.store.seed_history(BUNDLED_DATA_DIR / "claims_history_seed.json", date.today())
    state.engine = PolicyEngine()
    state.pipeline = ClaimPipeline(settings, state.store, state.engine)
    log.info("API ready (data dir %s, groq configured: %s)", settings.runtime_dir, bool(settings.groq_api_key))
    yield


app = FastAPI(title="Plum OPD Claim Adjudication API", version=VERSION, lifespan=lifespan)

_boot = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_boot.frontend_origins or ["http://localhost:3000"],
    # Vercel production + preview URLs are allowed by default so a fresh deploy works without
    # redeploying the API. Override with FRONTEND_ORIGIN_REGEX.
    allow_origin_regex=_boot.frontend_origin_regex or r"https://[a-z0-9-]+\.vercel\.app",
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Reviewer-Token"],
)


# --------------------------------------------------------------------- limits
_hits: dict[str, deque[float]] = defaultdict(deque)


def _rate_limit(request: Request) -> None:
    limit = get_settings().rate_limit_per_10_min
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > 600:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Too many claims submitted from this address. Please wait a few minutes.")
    q.append(now)


# --------------------------------------------------------------------- public
@app.get("/")
async def root() -> dict[str, Any]:
    return {"service": "Plum OPD Claim Adjudication API", "version": VERSION, "docs": "/docs", "health": "/health"}


@app.get("/health")
async def health() -> dict[str, Any]:
    s = get_settings()
    return {
        "status": "ok",
        "version": VERSION,
        "groq_configured": bool(s.groq_api_key),
        "vision_model": s.vision_model,
        "text_model": s.text_model,
        "reviewer_actions_enabled": bool(s.reviewer_token),
        "agents": ["Intake Agent", "OCR Agent", "Extraction Agent", "Eligibility Agent",
                   "Medical Review Agent", "Fraud & Risk Agent", "Policy Engine"],
        "final_authority": "Policy Engine (deterministic)",
    }


@app.get("/api/policy")
async def policy() -> dict[str, Any]:
    return state.engine.policy


@app.get("/api/members")
async def members() -> dict[str, Any]:
    regs = state.pipeline.eligibility.members
    return {"members": [{"member_id": m.member_id, "member_name": m.member_name, "active": m.active,
                         "join_date": m.join_date.isoformat()} for m in regs.values()]}


@app.get("/api/samples")
async def list_samples() -> dict[str, Any]:
    return {"samples": [samples.describe(s) for s in samples.SAMPLES]}


@app.get("/api/samples/{sample_id}/files/{index}")
async def sample_file(sample_id: str, index: int) -> Response:
    sample = samples.BY_ID.get(sample_id)
    if sample is None:
        raise HTTPException(404, "Sample not found.")
    files = samples.build_files(sample)
    if not 0 <= index < len(files):
        raise HTTPException(404, "Sample file not found.")
    name, mime, content = files[index]
    return Response(content, media_type=mime, headers={"Content-Disposition": f'inline; filename="{name}"',
                                                        "Cache-Control": "no-store"})


# --------------------------------------------------------------------- claims
@app.post("/api/claims")
async def submit_claim(
    request: Request,
    member_id: str = Form(..., min_length=2, max_length=32),
    member_name: str = Form(..., min_length=2, max_length=120),
    treatment_date: date = Form(...),
    claim_amount: float = Form(..., gt=0, le=10_000_000),
    hospital: str = Form("", max_length=160),
    cashless_request: bool = Form(False),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    _rate_limit(request)
    uploads = [(f.filename or "document", await f.read()) for f in files]
    form = ClaimForm(member_id=member_id.strip().upper(), member_name=member_name.strip(),
                     treatment_date=treatment_date, claim_amount=round(claim_amount, 2),
                     hospital=hospital.strip() or None, cashless_request=cashless_request)
    try:
        return await state.pipeline.process(form, uploads)
    except IntakeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # never leak internals
        log.exception("Claim processing failed")
        raise HTTPException(500, "Claim processing failed unexpectedly. Please retry.") from exc


@app.get("/api/claims")
async def list_claims(limit: int = 50, decision: str | None = None) -> dict[str, Any]:
    if decision and decision not in {"APPROVED", "PARTIAL", "REJECTED", "MANUAL_REVIEW"}:
        raise HTTPException(400, "Unknown decision filter.")
    return {"claims": state.store.list(limit, decision)}


@app.get("/api/claims/{claim_id}")
async def get_claim(claim_id: str) -> dict[str, Any]:
    found = state.store.get(claim_id)
    if found is None:
        raise HTTPException(404, "Claim not found.")
    return found[0]


@app.get("/api/stats")
async def stats() -> dict[str, Any]:
    return state.store.stats()


# --------------------------------------------------------------------- review
class ReviewRequest(BaseModel):
    action: Literal["APPROVE", "REJECT"]
    note: str = Field("", max_length=1000)


@app.post("/api/claims/{claim_id}/review")
async def review_claim(claim_id: str, payload: ReviewRequest,
                       x_reviewer_token: str | None = Header(default=None)) -> dict[str, Any]:
    expected = get_settings().reviewer_token
    if not expected:
        raise HTTPException(503, "Reviewer actions are disabled on this server (REVIEWER_TOKEN is not set).")
    if not x_reviewer_token or not secrets.compare_digest(x_reviewer_token, expected):
        raise HTTPException(401, "A valid reviewer token is required.")
    found = state.store.get(claim_id)
    if found is None:
        raise HTTPException(404, "Claim not found.")
    result, facts_json = found
    if result["decision"] != "MANUAL_REVIEW":
        raise HTTPException(409, f"Only MANUAL_REVIEW claims can be reviewed (this one is {result['decision']}).")
    note = payload.note.strip()
    if payload.action == "REJECT" and not note:
        raise HTTPException(400, "A note is required when rejecting a claim.")
    if payload.action == "APPROVE" and facts_json is None:
        raise HTTPException(409, "This claim has no stored evidence to re-evaluate.")

    result.pop("reviews", None)
    if payload.action == "APPROVE":
        # The reviewer clears the escalation; the engine still computes the amount and can still reject.
        rerun = state.engine.adjudicate(facts_from_dict(facts_json), claim_id=claim_id, reviewer_override=True)
        for key in ("decision", "approved_amount", "deduction", "deductions", "rejection_reasons", "reasons",
                    "notes", "next_steps", "checks", "rejected_items", "cashless_approved", "network_discount"):
            result[key] = rerun[key]
        result["review_reasons"] = []
        summary = f"Reviewer approved; engine recomputed {rerun['decision']} ₹{rerun['approved_amount']:,.0f}."
    else:
        result.update({"decision": "REJECTED", "approved_amount": 0.0, "deduction": result["claimed_amount"],
                       "deductions": {}, "review_reasons": [], "rejection_reasons": ["REVIEWER_REJECTED"],
                       "reasons": [note], "notes": f"Rejected by claims reviewer: {note}",
                       "next_steps": "Contact support if you believe this decision is wrong."})
        summary = "Reviewer rejected the claim."
    result["trace"].append({"agent": "Human Reviewer", "kind": "human", "status": "ok", "duration_ms": 0,
                            "summary": summary + (f" Note: {note}" if note else ""), "details": {}})
    result["reviewed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state.store.update_result(claim_id, result, payload.action, note)
    return state.store.get(claim_id)[0]
