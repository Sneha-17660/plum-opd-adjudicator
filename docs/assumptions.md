# Assumptions and design decisions

## Architecture

The system uses a **multi-agent pipeline** where LLM agents produce evidence and a **deterministic policy engine** makes every decision. This separation means:

- The same facts always produce the same decision (testable, auditable).
- LLM outputs can only raise the risk level (escalate to manual review), never lower it.
- A reviewer approving a manual-review claim still gets the amount recomputed by the engine.

## Acceptance suite

- **TC001:** ₹1,350 = ₹1,500 less 10% consultation co-pay.
- **TC002:** Root canal (₹8,000) approved; teeth whitening excluded as cosmetic. Decision: PARTIAL.
- **TC003:** ₹7,500 exceeds the ₹5,000 per-claim limit. Rejected.
- **TC006:** AYUR/KL/... registration accepted for alternative medicine.
- **TC008:** 3 prior same-day claims trigger the fraud threshold → MANUAL_REVIEW.
- **TC010:** Apollo Hospitals is a network provider; 20% discount applied to ₹4,500 → ₹3,600.

## Document dates

A follow-up date on a prescription (e.g. "Next visit: 08/11/2024") is stored separately in `follow_up_date` and is never compared against the treatment date on the claim form. Only `document_date` (the consultation/visit/bill date) is checked for consistency.

## Low-confidence extraction

When the engine would reject a claim based on extracted values (e.g. missing registration, date mismatch) but extraction confidence is below the configured threshold (70%), the rejection is converted to MANUAL_REVIEW so a human can verify the documents.

## Prompt injection

Text embedded in a document that addresses the AI or the claims system is:
1. Transcribed verbatim by the OCR agent.
2. Flagged by the extraction agent and the deterministic injection scanner.
3. Recorded in the claim result.
4. Used only to escalate the claim to MANUAL_REVIEW.

It never changes a policy rule or the adjudication outcome.

## Member registry

The supplied `members.json` is the trusted source of member identity. The document pipeline extracts a patient name but the **eligibility check** comes from the registry lookup, not from what the document says.

## Confidence score

The confidence score reflects how reliably the documents were read (OCR legibility × field-level extraction confidence), not whether the claim passed or failed. A clearly-read excluded treatment is a confident rejection.
