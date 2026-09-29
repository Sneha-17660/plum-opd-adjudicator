'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { API_BASE, api, inr, label } from '../lib/api';
import type { Check, ClaimResult, ClaimRow, Decision, Health, Sample, Stats } from '../lib/types';

type Tab = 'new' | 'claims' | 'review' | 'how';
type FormState = { member_id: string; member_name: string; treatment_date: string; claim_amount: string; hospital: string; cashless_request: boolean };

const EMPTY_FORM: FormState = { member_id: '', member_name: '', treatment_date: '', claim_amount: '', hospital: '', cashless_request: false };
const ACCEPT = '.pdf,.png,.jpg,.jpeg,.webp';
const MAX_MB = 10;
const STAGES = ['Checking files', 'Reading documents (OCR)', 'Extracting facts', 'Eligibility, medical and fraud checks', 'Applying the policy'];
const DECISION_TEXT: Record<Decision, string> = { APPROVED: 'Approved', PARTIAL: 'Partly approved', REJECTED: 'Rejected', MANUAL_REVIEW: 'Needs review' };

export default function Home() {
  const [tab, setTab] = useState<Tab>('new');
  const [health, setHealth] = useState<Health | null>(null);
  const [waking, setWaking] = useState(false);
  const [apiDown, setApiDown] = useState('');
  const [stats, setStats] = useState<Stats | null>(null);
  const [samples, setSamples] = useState<Sample[]>([]);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [files, setFiles] = useState<File[]>([]);
  const [sampleNote, setSampleNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState(0);
  const [error, setError] = useState('');
  const [result, setResult] = useState<ClaimResult | null>(null);
  const [rows, setRows] = useState<ClaimRow[]>([]);
  const [rowsLoading, setRowsLoading] = useState(false);
  const [openClaim, setOpenClaim] = useState<ClaimResult | null>(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const refreshStats = useCallback(async () => {
    try { setStats(await api('/api/stats') as Stats); } catch { /* shown via health banner */ }
  }, []);

  // Free hosting sleeps when idle: keep polling /health until the API is awake.
  useEffect(() => {
    let cancelled = false;
    const slow = setTimeout(() => !cancelled && setWaking(true), 2500);
    (async () => {
      const started = Date.now();
      while (!cancelled && Date.now() - started < 120_000) {
        try {
          const h = await api('/health', { timeoutMs: 20_000 }) as Health;
          if (cancelled) return;
          setHealth(h); setWaking(false); setApiDown('');
          refreshStats();
          api('/api/samples').then((d) => !cancelled && setSamples((d as { samples: Sample[] }).samples)).catch(() => {});
          return;
        } catch {
          await new Promise((r) => setTimeout(r, 4000));
        }
      }
      if (!cancelled) { setWaking(false); setApiDown(`The claims API at ${API_BASE} is not responding.`); }
    })();
    return () => { cancelled = true; clearTimeout(slow); };
  }, [refreshStats]);

  useEffect(() => {
    if (!busy) return;
    setStage(0);
    const t = setInterval(() => setStage((s) => Math.min(s + 1, STAGES.length - 1)), 3500);
    return () => clearInterval(t);
  }, [busy]);

  const loadRows = useCallback(async (decision?: Decision) => {
    setRowsLoading(true);
    try {
      const d = await api(decision ? `/api/claims?decision=${decision}&limit=100` : '/api/claims?limit=100') as { claims: ClaimRow[] };
      setRows(d.claims);
    } catch (e) { setError((e as Error).message); } finally { setRowsLoading(false); }
  }, []);

  useEffect(() => {
    if (tab === 'claims') loadRows();
    if (tab === 'review') loadRows('MANUAL_REVIEW');
    setOpenClaim(null);
  }, [tab, loadRows]);

  function addFiles(list: FileList | File[]) {
    const incoming = Array.from(list);
    const bad = incoming.filter((f) => !/\.(pdf|png|jpe?g|webp)$/i.test(f.name) || f.size > MAX_MB * 1024 * 1024);
    setError(bad.length ? `Skipped ${bad.map((f) => f.name).join(', ')}: use PDF, JPG, PNG or WEBP up to ${MAX_MB} MB.` : '');
    setFiles((prev) => [...prev, ...incoming.filter((f) => !bad.includes(f) && !prev.some((p) => p.name === f.name && p.size === f.size))].slice(0, 6));
    setResult(null);
  }

  async function loadSample(s: Sample) {
    setError(''); setResult(null); setSampleNote('Loading sample documents…');
    try {
      const blobs = await Promise.all(s.files.map((f) => api(`/api/samples/${s.id}/files/${f.index}`) as Promise<Blob>));
      setFiles(blobs.map((b, i) => new File([b], s.files[i].name, { type: b.type })));
      setForm({ ...s.form, claim_amount: String(s.form.claim_amount) });
      setSampleNote(`Sample loaded: ${s.title}. Expected outcome: ${s.expected}.`);
    } catch (e) { setSampleNote(''); setError((e as Error).message); }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    if (!files.length) { setError('Add the prescription and the bill before submitting.'); return; }
    setBusy(true); setError(''); setResult(null);
    const body = new FormData();
    (Object.keys(form) as (keyof FormState)[]).forEach((k) => body.append(k, String(form[k])));
    files.forEach((f) => body.append('files', f, f.name));
    try {
      const r = await api('/api/claims', { method: 'POST', body }) as ClaimResult;
      setResult(r); refreshStats();
      requestAnimationFrame(() => document.getElementById('result')?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  function reset() { setForm(EMPTY_FORM); setFiles([]); setResult(null); setError(''); setSampleNote(''); }

  async function openRow(id: string) {
    try { setOpenClaim(await api(`/api/claims/${encodeURIComponent(id)}`) as ClaimResult); } catch (e) { setError((e as Error).message); }
  }

  function onReviewed(updated: ClaimResult) {
    setOpenClaim(updated);
    if (result?.claim_id === updated.claim_id) setResult(updated);
    refreshStats();
    if (tab === 'review') loadRows('MANUAL_REVIEW');
  }

  return (
    <div className="shell">
      <header className="masthead">
        <div className="brand">
          <span className="mark" aria-hidden>p</span>
          <div><strong>OPD claim desk</strong><small>Plum OPD Advantage · policy PLUM_OPD_2024</small></div>
        </div>
        <nav aria-label="Sections">
          {([['new', 'New claim'], ['claims', 'Claims'], ['review', 'Review queue'], ['how', 'How decisions are made']] as [Tab, string][]).map(([k, t]) => (
            <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)} aria-current={tab === k ? 'page' : undefined}>
              {t}{k === 'review' && stats?.manual_review ? <span className="count">{stats.manual_review}</span> : null}
            </button>
          ))}
        </nav>
        <ApiStatus health={health} waking={waking} down={apiDown} />
      </header>

      {waking && <div className="banner info">Starting the claims API. Free hosting sleeps when idle, so the first request can take up to a minute.</div>}
      {apiDown && <div className="banner bad">{apiDown} Check that the backend is deployed and NEXT_PUBLIC_API_URL points to it.</div>}
      {health && !health.groq_configured && <div className="banner warn">The API has no GROQ_API_KEY, so document reading is unavailable. Samples and history still load.</div>}
      {error && <div className="banner bad" role="alert"><span>{error}</span><button onClick={() => setError('')} aria-label="Dismiss">×</button></div>}

      {tab === 'new' && (
        <main className="desk">
          <section className="panel intake" aria-labelledby="intake-h">
            <h1 id="intake-h">Submit an OPD claim</h1>
            <p className="lede">Upload the prescription and the bill. Six agents read and check them; the policy engine makes the decision.</p>

            {samples.length > 0 && (
              <div className="samples">
                <h2>Try a sample claim</h2>
                <p className="hint">Synthetic documents, freshly dated. Each shows a different policy rule.</p>
                <div className="sample-grid">
                  {samples.map((s) => (
                    <button key={s.id} type="button" onClick={() => loadSample(s)} disabled={busy} title={s.expected}>
                      <span>{s.title}</span><small>{s.expected}</small>
                    </button>
                  ))}
                </div>
                {sampleNote && <p className="note" aria-live="polite">{sampleNote}</p>}
              </div>
            )}

            <form onSubmit={submit} className="claim-form">
              <div className="fields">
                <Field label="Member ID"><input required value={form.member_id} onChange={(e) => setForm({ ...form, member_id: e.target.value })} placeholder="EMP001" autoComplete="off" /></Field>
                <Field label="Member name"><input required value={form.member_name} onChange={(e) => setForm({ ...form, member_name: e.target.value })} placeholder="As on the policy" /></Field>
                <Field label="Treatment date"><input required type="date" value={form.treatment_date} onChange={(e) => setForm({ ...form, treatment_date: e.target.value })} /></Field>
                <Field label="Amount claimed (₹)"><input required type="number" min="1" step="0.01" inputMode="decimal" value={form.claim_amount} onChange={(e) => setForm({ ...form, claim_amount: e.target.value })} placeholder="1500" /></Field>
                <Field label="Hospital or clinic (optional)" wide><input value={form.hospital} onChange={(e) => setForm({ ...form, hospital: e.target.value })} placeholder="Needed for network / cashless claims" /></Field>
                <label className="check wide"><input type="checkbox" checked={form.cashless_request} onChange={(e) => setForm({ ...form, cashless_request: e.target.checked })} /> Request cashless settlement (network hospitals only)</label>
              </div>

              <div
                className={`drop ${dragging ? 'over' : ''}`}
                onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
                onDragLeave={() => setDragging(false)}
                onDrop={(e) => { e.preventDefault(); setDragging(false); addFiles(e.dataTransfer.files); }}
              >
                <input ref={inputRef} type="file" multiple accept={ACCEPT} hidden onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ''; }} />
                <p><strong>Drop documents here</strong> or <button type="button" className="link" onClick={() => inputRef.current?.click()}>choose files</button></p>
                <small>Prescription, bills, reports. PDF or photo, up to {MAX_MB} MB each, 6 files.</small>
              </div>
              {files.length > 0 && (
                <ul className="files">
                  {files.map((f) => (
                    <li key={f.name + f.size}>
                      <span className="ext">{f.name.split('.').pop()}</span>
                      <span className="fname">{f.name}</span>
                      <small>{Math.max(1, Math.round(f.size / 1024))} KB</small>
                      <button type="button" onClick={() => setFiles(files.filter((x) => x !== f))} aria-label={`Remove ${f.name}`}>Remove</button>
                    </li>
                  ))}
                </ul>
              )}

              <div className="actions">
                <button className="primary" type="submit" disabled={busy || !health}>{busy ? 'Processing…' : 'Submit claim'}</button>
                <button type="button" className="ghost" onClick={reset} disabled={busy}>Clear</button>
              </div>
            </form>

            {busy && (
              <ol className="progress" aria-live="polite">
                {STAGES.map((s, i) => <li key={s} className={i < stage ? 'done' : i === stage ? 'now' : ''}>{s}</li>)}
              </ol>
            )}
          </section>

          <section id="result" className="panel outcome" aria-live="polite">
            {result ? <ClaimView claim={result} reviewerEnabled={!!health?.reviewer_actions_enabled} onReviewed={onReviewed} />
              : <EmptyOutcome stats={stats} />}
          </section>
        </main>
      )}

      {(tab === 'claims' || tab === 'review') && (
        <main className="desk list">
          <section className="panel">
            <h1>{tab === 'claims' ? 'Processed claims' : 'Claims waiting for a reviewer'}</h1>
            {tab === 'review' && <p className="lede">These were escalated because of a risk signal or uncertain evidence. A reviewer can approve (the engine recomputes the amount) or reject with a note.</p>}
            {stats && tab === 'claims' && <StatsLine stats={stats} />}
            {rowsLoading ? <p className="muted">Loading…</p> : rows.length === 0 ? (
              <p className="muted">{tab === 'claims' ? 'No claims yet. Submit one from New claim.' : 'Nothing to review.'}</p>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Claim</th><th>Member</th><th>Treatment</th><th>Decision</th><th className="num">Claimed</th><th className="num">Payable</th></tr></thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.claim_id} onClick={() => openRow(r.claim_id)} className={openClaim?.claim_id === r.claim_id ? 'sel' : ''}>
                        <td><button className="link" onClick={(e) => { e.stopPropagation(); openRow(r.claim_id); }}>{r.claim_id}</button></td>
                        <td>{r.member_name || r.member_id}<small>{r.member_id}</small></td>
                        <td>{r.treatment_date}</td>
                        <td><span className={`pill ${r.decision}`}>{DECISION_TEXT[r.decision]}</span></td>
                        <td className="num">{inr(r.claimed_amount)}</td>
                        <td className="num">{inr(r.approved_amount)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          {openClaim && (
            <section className="panel outcome">
              <ClaimView claim={openClaim} reviewerEnabled={!!health?.reviewer_actions_enabled} onReviewed={onReviewed} />
            </section>
          )}
        </main>
      )}

      {tab === 'how' && <HowItWorks health={health} />}

      <footer className="foot">Synthetic demo data only. Not a real insurer. API: <a href={`${API_BASE}/docs`} target="_blank" rel="noreferrer">{API_BASE}/docs</a></footer>
    </div>
  );
}

function Field({ label: text, children, wide }: { label: string; children: React.ReactNode; wide?: boolean }) {
  return <label className={`field ${wide ? 'wide' : ''}`}><span>{text}</span>{children}</label>;
}

function ApiStatus({ health, waking, down }: { health: Health | null; waking: boolean; down: string }) {
  const state = health ? 'up' : down ? 'down' : waking ? 'waking' : 'checking';
  const text = { up: `API online · v${health?.version}`, down: 'API offline', waking: 'API starting…', checking: 'Connecting…' }[state];
  return <span className={`status ${state}`}><i aria-hidden />{text}</span>;
}

function StatsLine({ stats }: { stats: Stats }) {
  return (
    <dl className="stats">
      <div><dt>Claims</dt><dd>{stats.total}</dd></div>
      <div><dt>Approved</dt><dd>{stats.approved}</dd></div>
      <div><dt>Partly approved</dt><dd>{stats.partial}</dd></div>
      <div><dt>Rejected</dt><dd>{stats.rejected}</dd></div>
      <div><dt>Needs review</dt><dd>{stats.manual_review}</dd></div>
      <div><dt>Paid out</dt><dd>{inr(stats.total_approved)}</dd></div>
    </dl>
  );
}

function EmptyOutcome({ stats }: { stats: Stats | null }) {
  return (
    <div className="empty">
      <h2>The decision appears here</h2>
      <p>You will see the outcome, the payable amount with every deduction, each policy check, and a timeline of what each agent did.</p>
      <p className="muted">Pick a sample on the left to see a full run in about 20 seconds.</p>
      {stats && stats.total > 0 && <StatsLine stats={stats} />}
    </div>
  );
}

const STATUS_MARK: Record<Check['status'], string> = { pass: '✓', fail: '✕', warn: '!', info: 'i' };

function ClaimView({ claim, reviewerEnabled, onReviewed }: { claim: ClaimResult; reviewerEnabled: boolean; onReviewed: (c: ClaimResult) => void }) {
  const steps = Array.from(new Set(claim.checks.map((c) => c.step)));
  const codes = [...claim.rejection_reasons, ...claim.review_reasons];
  return (
    <article className="claim">
      <header className="verdict">
        <div className={`stamp ${claim.decision}`}>{DECISION_TEXT[claim.decision]}</div>
        <div className="amounts">
          <p className="big">{inr(claim.approved_amount)}</p>
          <p className="muted">payable of {inr(claim.claimed_amount)} claimed</p>
        </div>
        <dl className="meta">
          <div><dt>Claim</dt><dd>{claim.claim_id}</dd></div>
          <div><dt>Member</dt><dd>{claim.form.member_name} ({claim.form.member_id})</dd></div>
          <div><dt>Treatment</dt><dd>{claim.form.treatment_date}</dd></div>
          <div><dt>Evidence confidence</dt><dd>{Math.round(claim.confidence_score * 100)}%</dd></div>
        </dl>
      </header>

      <p className="notes">{claim.notes}</p>
      {codes.length > 0 && <p className="codes">{codes.map((c) => <code key={c}>{c}</code>)}</p>}
      {claim.reasons.length > 1 && <ul className="reasons">{claim.reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
      <p className="next"><strong>Next step:</strong> {claim.next_steps}</p>
      {claim.cashless_approved && <p className="good-note">Cashless settlement approved at {claim.network_provider}.</p>}

      {claim.decision === 'MANUAL_REVIEW' && <ReviewBox claim={claim} enabled={reviewerEnabled} onReviewed={onReviewed} />}
      {claim.reviews && claim.reviews.length > 0 && (
        <div className="reviewed">{claim.reviews.map((r) => <p key={r.reviewed_at}>Reviewer {r.action.toLowerCase()}d on {new Date(r.reviewed_at).toLocaleString()}{r.note ? `: “${r.note}”` : ''}</p>)}</div>
      )}

      <h3>How the amount was worked out</h3>
      <div className="table-wrap">
        <table className="money">
          <tbody>
            {claim.line_items.map((i, n) => (
              <tr key={n} className={claim.rejected_items.some((r) => r.startsWith(i.description)) ? 'struck' : ''}>
                <td>{i.description}<small>{i.category || 'uncategorised'} · {i.source_document}</small></td><td className="num">{inr(i.amount)}</td>
              </tr>
            ))}
            {Object.entries(claim.deductions || {}).map(([k, v]) => <tr key={k} className="minus"><td>Less {label(k)}</td><td className="num">−{inr(v)}</td></tr>)}
            <tr className="total"><td>Payable</td><td className="num">{inr(claim.approved_amount)}</td></tr>
          </tbody>
        </table>
      </div>

      <h3>Policy checks</h3>
      {steps.map((s) => (
        <div key={s} className="step-group">
          <h4>{s}</h4>
          <ul className="checks">
            {claim.checks.filter((c) => c.step === s).map((c, n) => (
              <li key={n} className={c.status}><b aria-label={c.status}>{STATUS_MARK[c.status]}</b><span><strong>{c.name}</strong> {c.detail}</span></li>
            ))}
          </ul>
        </div>
      ))}

      <h3>What each agent did</h3>
      <ol className="trace">
        {claim.trace.map((t, n) => (
          <li key={n} className={t.status}>
            <div className="trace-head"><strong>{t.agent}</strong><span className={`kind ${t.kind}`}>{t.kind === 'llm' ? 'AI model' : t.kind === 'human' ? 'human' : 'rules'}</span><small>{(t.duration_ms / 1000).toFixed(1)} s</small></div>
            <p>{t.summary}</p>
          </li>
        ))}
      </ol>
      <p className="muted small">Final authority: {claim.final_authority}. AI agents only supply evidence; they cannot approve or reject.</p>

      <h3>What was read from the documents</h3>
      {claim.documents.map((d) => (
        <details key={d.file_name} className="doc">
          <summary><span className="doctype">{label(d.document_type)}</span> {d.file_name}<small>legibility {Math.round(d.legibility * 100)}%</small></summary>
          <dl className="facts">
            {([['Patient', d.patient_name, 'patient_name'], ['Provider', d.provider_name, ''], ['Doctor', d.doctor_name, ''],
              ['Registration', d.doctor_registration, 'doctor_registration'], ['Date', d.document_date, 'document_date'],
              ['Follow-up', d.follow_up_date, ''], ['Diagnosis', d.diagnosis, 'diagnosis'], ['Invoice', d.invoice_number, ''],
              ['Total', d.total_amount != null ? inr(d.total_amount) : null, 'total_amount'], ['Medicines', d.medicines.join(', '), ''],
              ['Tests', d.tests.join(', '), ''], ['Treatments', d.treatments.join(', '), '']] as [string, string | null, string][])
              .filter(([, v]) => v).map(([k, v, key]) => (
                <div key={k}><dt>{k}</dt><dd>{v}{key && d.field_confidence[key] != null && <small> read with {Math.round(d.field_confidence[key] * 100)}% confidence</small>}</dd></div>
              ))}
          </dl>
          {[...d.visual_flags, ...d.injection_text, ...d.extraction_warnings].length > 0 && (
            <ul className="warnings">{[...d.visual_flags.map((x) => `Visual: ${x}`), ...d.injection_text.map((x) => `Ignored instruction: “${x}”`), ...d.extraction_warnings].map((w) => <li key={w}>{w}</li>)}</ul>
          )}
          <pre className="transcript">{d.transcript_preview || 'No transcript.'}</pre>
        </details>
      ))}
    </article>
  );
}

function ReviewBox({ claim, enabled, onReviewed }: { claim: ClaimResult; enabled: boolean; onReviewed: (c: ClaimResult) => void }) {
  const [token, setToken] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  useEffect(() => { try { setToken(sessionStorage.getItem('reviewer-token') || ''); } catch { /* storage unavailable */ } }, []);

  async function act(action: 'APPROVE' | 'REJECT') {
    setBusy(true); setErr('');
    try { sessionStorage.setItem('reviewer-token', token); } catch { /* ignore */ }
    try {
      const updated = await api(`/api/claims/${encodeURIComponent(claim.claim_id)}/review`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Reviewer-Token': token },
        body: JSON.stringify({ action, note }),
      }) as ClaimResult;
      onReviewed(updated);
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }

  if (!enabled) return <div className="review-box"><p className="muted">Reviewer actions are switched off on this server.</p></div>;
  return (
    <div className="review-box">
      <h3>Reviewer decision</h3>
      <p className="muted small">Approving clears the escalation only. The policy engine recomputes the payable amount and can still reject.</p>
      <div className="fields">
        <Field label="Reviewer token"><input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" /></Field>
        <Field label="Note (required to reject)" wide><input value={note} onChange={(e) => setNote(e.target.value)} maxLength={1000} /></Field>
      </div>
      {err && <p className="err">{err}</p>}
      <div className="actions">
        <button className="primary" disabled={busy || !token} onClick={() => act('APPROVE')}>Approve</button>
        <button className="danger" disabled={busy || !token || !note.trim()} onClick={() => act('REJECT')}>Reject</button>
      </div>
    </div>
  );
}

function HowItWorks({ health }: { health: Health | null }) {
  const agents: [string, string, string][] = [
    ['Intake Agent', 'rules', 'Checks file type by content, size and count; drops duplicate files; renders PDF pages to images and keeps the PDF text layer.'],
    ['OCR Agent', 'AI model', 'A vision model transcribes each file verbatim, rates legibility, and reports visible tampering and any text addressed to an AI.'],
    ['Extraction Agent', 'AI model', 'Turns each transcript into typed facts with per-field confidence. A grounding pass lowers confidence for any value not found in the document text.'],
    ['Eligibility Agent', 'rules', 'Looks up the member, join date and pre-authorizations in trusted records, and builds claim history for the year.'],
    ['Medical Review Agent', 'AI model', 'Judges whether the treatment fits the diagnosis. It can only raise a concern for a human; it cannot reject.'],
    ['Fraud & Risk Agent', 'rules', 'Duplicate bills, same-day and high-frequency claims, tampering signs and embedded instructions.'],
    ['Policy Engine', 'rules', 'The only component that decides: eligibility, documents, exclusions, pre-authorization, sub-limits, per-claim and annual limits, co-pay and network discount.'],
  ];
  return (
    <main className="desk how">
      <section className="panel">
        <h1>How decisions are made</h1>
        <p className="lede">AI reads the documents. Rules decide the claim. People review anything uncertain.</p>
        <ol className="pipeline">
          {agents.map(([n, k, d]) => <li key={n}><div><strong>{n}</strong><span className={`kind ${k === 'AI model' ? 'llm' : 'deterministic'}`}>{k}</span></div><p>{d}</p></li>)}
        </ol>
        <h2>Safeguards</h2>
        <ul className="plain">
          <li>Text in a document that tries to instruct the system is recorded and sends the claim to a reviewer. It never changes a rule.</li>
          <li>A rejection that depends on low-confidence reading becomes a manual review instead.</li>
          <li>Follow-up dates on a prescription are kept separate from the visit date, so they never cause a date mismatch.</li>
          <li>Reviewer actions need a server-side token, and the amount is always recomputed by the engine.</li>
        </ul>
        {health && <p className="muted small">Models: {health.vision_model} (OCR), {health.text_model} (extraction and medical review).</p>}
      </section>
    </main>
  );
}
