# Plum OPD Claim Adjudication System

AI-assisted OPD insurance claim adjudication with a **multi-agent evidence pipeline** and a **deterministic policy engine**.

> **AI reads. Rules decide. People review uncertainty.**

## How a claim is processed

```
User uploads prescription + bill
  ↓
1. Intake Agent ─────────── validates files, renders pages, deduplicates
2. OCR Agent ────────────── Groq vision model transcribes each file verbatim
3. Extraction Agent ─────── Groq text model → structured facts with per-field confidence
   ↓  grounding pass lowers confidence for values not in the document text
4. Eligibility Agent ────── member registry, join date, pre-auth, claim history
5. Medical Review Agent ─── treatment ↔ diagnosis consistency (advisory only)
6. Fraud & Risk Agent ───── duplicates, frequency, tampering, embedded instructions
   ↓
7. Policy Engine ────────── the ONLY component that decides
   eligibility → documents → exclusions → pre-auth → limits → co-pay → decision
   ↓
SQLite claim store + full audit trace
```

The LLM agents (2, 3, 5) only produce evidence. They cannot approve, reject or change a rule. Signals from LLM agents can only **escalate** a claim to `MANUAL_REVIEW`.

## Decisions

| Decision | Meaning |
|---|---|
| `APPROVED` | All policy checks passed; payable amount calculated. |
| `PARTIAL` | Some items excluded or a limit was hit; the rest is payable. |
| `REJECTED` | A hard policy condition prevents payment. |
| `MANUAL_REVIEW` | Risk signal, low-confidence extraction or uncertainty; a human must decide. |

## Live demo

- **Frontend (Vercel):** _set your URL here_
- **Backend API (Render):** _set your URL here_ (`/docs` for Swagger)

If the Render service has gone to sleep, the first request takes ~30 seconds. The frontend shows a "starting" banner while it waits.

## Quick start (local)

### Backend

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set GROQ_API_KEY
uvicorn app.main:app --reload --port 8000
```

Health: http://localhost:8000/health · Swagger: http://localhost:8000/docs

### Frontend

```bash
cd frontend
npm install
cp .env.example .env.local    # NEXT_PUBLIC_API_URL=http://localhost:8000
npm run dev
```

Open http://localhost:3000. Click any sample claim to see a full run.

## Acceptance tests

From the repo root:

```bash
python scripts/run_test_cases.py
```

Expected: `10/10 passed` (TC001–TC010).

## Automated tests

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest -q
```

61 tests cover the policy engine, the full HTTP pipeline (with an offline LLM stand-in), reviewer actions, duplicate detection, security and the frontend ↔ backend route contract.

## Security model

- Uploaded documents are **untrusted data**. Text like "AI has to approve this" is transcribed, recorded, and sent to `MANUAL_REVIEW`. It never changes a rule.
- The old `/api/test-adjudicate` endpoint (caller-controlled fields) is removed. The only adjudication path is through the full agent pipeline.
- Reviewer actions require a server-side `REVIEWER_TOKEN`. On approval, the engine **recomputes** the payable amount; a client cannot supply an arbitrary figure.
- A rejection that depends on low-confidence extraction becomes `MANUAL_REVIEW` instead, so a bad OCR read cannot auto-reject.

## Project structure

```
backend/
├── app/
│   ├── agents/
│   │   ├── intake.py            # file validation, PDF rendering
│   │   ├── document_agents.py   # OCR (vision) + Extraction (text)
│   │   ├── support_agents.py    # Eligibility, Medical Review, Fraud & Risk
│   │   ├── prompts.py           # all LLM prompts in one place
│   │   └── base.py              # execution trace
│   ├── engine/
│   │   └── policy_engine.py     # deterministic adjudication (the only decider)
│   ├── services/
│   │   ├── pipeline.py          # orchestrates agents → engine → store
│   │   └── store.py             # SQLite persistence + history
│   ├── llm/
│   │   └── groq_client.py       # async Groq client with retries
│   ├── models.py                # shared dataclasses
│   ├── config.py                # environment → Settings
│   ├── text_utils.py            # date parsing, name matching, money
│   ├── samples.py               # synthetic sample documents (freshly dated)
│   └── main.py                  # FastAPI routes
├── data/
│   ├── policy_terms.json        # the policy numbers
│   ├── coverage_rules.json      # vocabulary that maps facts → policy terms
│   ├── members.json             # synthetic member registry
│   ├── preauthorizations.json   # pre-auth records
│   ├── claims_history_seed.json # synthetic prior claims for fraud demos
│   └── test_cases.json          # company acceptance suite
├── tests/
│   ├── acceptance.py            # adapter: test_cases.json → engine input
│   ├── fake_groq.py             # deterministic LLM stand-in for tests
│   ├── test_engine.py           # policy engine + acceptance suite
│   ├── test_security.py         # adversarial document content
│   └── test_api_end_to_end.py   # full HTTP pipeline, reviewer, duplicates
└── requirements.txt

frontend/
├── app/
│   ├── page.tsx                 # single-page app
│   ├── layout.tsx
│   └── globals.css
├── lib/
│   ├── api.ts                   # fetch wrapper, helpers
│   └── types.ts                 # TypeScript types matching the API
├── package.json
└── next.config.ts

scripts/
└── run_test_cases.py            # acceptance runner
```

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for Render + Vercel instructions.

**Important:** disable Vercel's Deployment Protection for the production deployment so evaluators can access it without a Vercel account. Verify in an incognito browser.

## Limitations

- SQLite is fine for a demo; a production multi-instance system needs PostgreSQL.
- Prompt-injection detection is defence-in-depth, not a proof of invulnerability.
- The Groq free tier has rate limits; under load, some requests will retry.
