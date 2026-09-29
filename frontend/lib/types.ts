export type Decision = 'APPROVED' | 'PARTIAL' | 'REJECTED' | 'MANUAL_REVIEW';

export type Check = { step: string; name: string; status: 'pass' | 'fail' | 'warn' | 'info'; detail: string; code: string | null };
export type TraceEntry = { agent: string; kind: 'llm' | 'deterministic' | 'human'; status: 'ok' | 'warning' | 'error'; duration_ms: number; summary: string; details?: Record<string, unknown> };
export type LineItem = { description: string; amount: number; category: string | null; source_document: string };
export type DocumentFacts = {
  file_name: string; document_type: string; patient_name: string | null; provider_name: string | null;
  doctor_name: string | null; doctor_registration: string | null; document_date: string | null; follow_up_date: string | null;
  diagnosis: string | null; medicines: string[]; tests: string[]; treatments: string[]; total_amount: number | null;
  invoice_number: string | null; legibility: number; handwritten: boolean; visual_flags: string[]; injection_text: string[];
  extraction_warnings: string[]; field_confidence: Record<string, number>; transcript_preview: string;
};
export type Review = { reviewed_at: string; action: string; note: string; resulting_decision: Decision };

export type ClaimResult = {
  claim_id: string; decision: Decision; approved_amount: number; claimed_amount: number; deduction: number;
  deductions: Record<string, number>; rejection_reasons: string[]; review_reasons: string[]; flags: string[];
  reasons: string[]; confidence_score: number; notes: string; next_steps: string; checks: Check[];
  rejected_items: string[]; line_items: LineItem[]; primary_category: string; network_provider: string | null;
  cashless_approved: boolean; network_discount: number; processed_at: string;
  form: { member_id: string; member_name: string; treatment_date: string; claim_amount: number; hospital: string | null; cashless_request: boolean };
  member: { member_id: string; member_name: string; join_date: string } | null;
  provider: string | null; documents: DocumentFacts[]; medical_review: { available: boolean; consistent: boolean | null; rationale: string; concerns: string[] };
  extraction_confidence: number; trace: TraceEntry[]; final_authority: string; reviews?: Review[];
};

export type ClaimRow = {
  claim_id: string; created_at: string; member_id: string; member_name: string | null; treatment_date: string;
  provider: string | null; decision: Decision; approved_amount: number; claimed_amount: number; confidence: number | null;
};
export type Stats = { total: number; approved: number; partial: number; rejected: number; manual_review: number; total_claimed: number; total_approved: number };
export type Sample = {
  id: string; title: string; expected: string;
  form: { member_id: string; member_name: string; treatment_date: string; claim_amount: number; hospital: string; cashless_request: boolean };
  files: { index: number; name: string }[];
};
export type Health = { status: string; version: string; groq_configured: boolean; vision_model: string; text_model: string; reviewer_actions_enabled: boolean; agents: string[] };
