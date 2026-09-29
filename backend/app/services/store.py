"""SQLite claim store: results, the facts they were decided on, reviews, history."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from app.models import HistoryContext

PAID = ("APPROVED", "PARTIAL")


class ClaimStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS claims (
              claim_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, member_id TEXT NOT NULL,
              member_name TEXT, treatment_date TEXT NOT NULL, provider TEXT,
              decision TEXT NOT NULL, approved_amount REAL NOT NULL, claimed_amount REAL NOT NULL,
              confidence REAL, doc_hashes TEXT NOT NULL DEFAULT '[]', invoice_keys TEXT NOT NULL DEFAULT '[]',
              seeded INTEGER NOT NULL DEFAULT 0, result_json TEXT NOT NULL, facts_json TEXT);
            CREATE INDEX IF NOT EXISTS idx_claims_member ON claims(member_id, treatment_date);
            CREATE TABLE IF NOT EXISTS reviews (
              id INTEGER PRIMARY KEY AUTOINCREMENT, claim_id TEXT NOT NULL, reviewed_at TEXT NOT NULL,
              action TEXT NOT NULL, note TEXT, resulting_decision TEXT NOT NULL);
            """)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------ seeding
    def seed_history(self, seed_file: Path, today: date) -> int:
        """(Re)load synthetic prior claims, resolving relative dates against today."""
        if not seed_file.exists():
            return 0
        entries = json.loads(seed_file.read_text(encoding="utf-8")).get("claims", [])
        with self._conn() as c:
            c.execute("DELETE FROM claims WHERE seeded = 1")
            for i, e in enumerate(entries):
                t = today - timedelta(days=int(e["treatment_days_ago"]))
                cid = f"SEED_{i + 1:03d}"
                result = {"claim_id": cid, "decision": e["decision"], "approved_amount": e["amount"],
                          "claimed_amount": e["amount"], "notes": "Synthetic prior claim (history seed).",
                          "form": {"member_id": e["member_id"], "treatment_date": t.isoformat()}}
                c.execute("""INSERT OR REPLACE INTO claims (claim_id, created_at, member_id, member_name, treatment_date,
                             provider, decision, approved_amount, claimed_amount, confidence, seeded, result_json)
                             VALUES (?,?,?,?,?,?,?,?,?,?,1,?)""",
                          (cid, _now(), e["member_id"], None, t.isoformat(), e.get("provider"), e["decision"],
                           e["amount"], e["amount"], None, json.dumps(result)))
        return len(entries)

    # ------------------------------------------------------------------ history
    def history_context(self, *, member_id: str, treatment_date: date, doc_hashes: list[str],
                        invoice_keys: list[str], exclude_claim_id: str | None = None) -> HistoryContext:
        start_of_year = date(treatment_date.year, 1, 1).isoformat()
        with self._conn() as c:
            rows = c.execute("SELECT claim_id, treatment_date, decision, approved_amount, doc_hashes, invoice_keys "
                             "FROM claims WHERE member_id = ? AND claim_id != ?",
                             (member_id, exclude_claim_id or "")).fetchall()
            others = c.execute("SELECT claim_id, doc_hashes, invoice_keys FROM claims "
                               "WHERE decision != 'REJECTED' AND claim_id != ?", (exclude_claim_id or "",)).fetchall()
        td = treatment_date.isoformat()
        window = (treatment_date - timedelta(days=30)).isoformat()
        ytd = sum(r["approved_amount"] for r in rows if r["decision"] in PAID and start_of_year <= r["treatment_date"] <= td)
        same_day = sum(1 for r in rows if r["treatment_date"] == td)
        last30 = sum(1 for r in rows if window <= r["treatment_date"] <= td)
        hashes, keys = set(doc_hashes), set(invoice_keys)
        dups = [r["claim_id"] for r in others
                if hashes & set(json.loads(r["doc_hashes"])) or keys & set(json.loads(r["invoice_keys"]))]
        return HistoryContext(round(ytd, 2), same_day, last30, dups)

    # --------------------------------------------------------------- read/write
    def save(self, result: dict[str, Any], facts_json: dict[str, Any], doc_hashes: list[str], invoice_keys: list[str]) -> None:
        form = result["form"]
        with self._conn() as c:
            c.execute("""INSERT OR REPLACE INTO claims (claim_id, created_at, member_id, member_name, treatment_date, provider,
                         decision, approved_amount, claimed_amount, confidence, doc_hashes, invoice_keys, seeded,
                         result_json, facts_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
                      (result["claim_id"], result["processed_at"], form["member_id"].upper(), form["member_name"],
                       form["treatment_date"], result.get("provider"), result["decision"], result["approved_amount"],
                       result["claimed_amount"], result["confidence_score"], json.dumps(doc_hashes),
                       json.dumps(invoice_keys), json.dumps(result, ensure_ascii=False), json.dumps(facts_json, ensure_ascii=False)))

    def update_result(self, claim_id: str, result: dict[str, Any], action: str, note: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE claims SET decision=?, approved_amount=?, result_json=? WHERE claim_id=?",
                      (result["decision"], result["approved_amount"], json.dumps(result, ensure_ascii=False), claim_id))
            c.execute("INSERT INTO reviews (claim_id, reviewed_at, action, note, resulting_decision) VALUES (?,?,?,?,?)",
                      (claim_id, _now(), action, note, result["decision"]))

    def get(self, claim_id: str) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
        with self._conn() as c:
            row = c.execute("SELECT result_json, facts_json FROM claims WHERE claim_id = ? AND seeded = 0", (claim_id,)).fetchone()
            if not row:
                return None
            reviews = [dict(r) for r in c.execute(
                "SELECT reviewed_at, action, note, resulting_decision FROM reviews WHERE claim_id = ? ORDER BY id", (claim_id,))]
        result = json.loads(row["result_json"])
        result["reviews"] = reviews
        return result, (json.loads(row["facts_json"]) if row["facts_json"] else None)

    def list(self, limit: int = 50, decision: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        q = ("SELECT claim_id, created_at, member_id, member_name, treatment_date, provider, decision, "
             "approved_amount, claimed_amount, confidence FROM claims WHERE seeded = 0")
        args: list[Any] = []
        if decision:
            q += " AND decision = ?"
            args.append(decision)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args)]

    def stats(self) -> dict[str, Any]:
        with self._conn() as c:
            rows = c.execute("SELECT decision, COUNT(*) n, COALESCE(SUM(claimed_amount),0) claimed, "
                             "COALESCE(SUM(approved_amount),0) paid FROM claims WHERE seeded = 0 GROUP BY decision").fetchall()
        by = {r["decision"]: r for r in rows}
        return {
            "total": sum(r["n"] for r in rows),
            "approved": by["APPROVED"]["n"] if "APPROVED" in by else 0,
            "partial": by["PARTIAL"]["n"] if "PARTIAL" in by else 0,
            "rejected": by["REJECTED"]["n"] if "REJECTED" in by else 0,
            "manual_review": by["MANUAL_REVIEW"]["n"] if "MANUAL_REVIEW" in by else 0,
            "total_claimed": round(sum(r["claimed"] for r in rows), 2),
            "total_approved": round(sum(r["paid"] for r in rows), 2),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
