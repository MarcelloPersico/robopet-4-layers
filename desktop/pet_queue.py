"""Pending-questions queue: SQLite schema, CRUD, frame snapshots, and the
resolved-knowledge table that feeds the recent-answers buffer. Plan §8.4, §5.5.

(Module named ``pet_queue`` rather than the plan's ``queue`` to avoid shadowing
Python's stdlib ``queue``, which ``concurrent.futures`` imports for every
``run_in_executor`` call. See CLAUDE.md.)

This is the heart of the "defer to a human" pattern that replaces automated
cloud escalation: the agent writes a row via :meth:`queue_question`; a human
(directly via cli_queue.py, or through Claude over MCP) resolves it; resolutions
shared with the robot are appended to ``resolved_knowledge`` and surfaced to the
agent through WorldState's recent-answers buffer.

Synchronous and thread-safe (single connection + lock); the async orchestrator
calls these via ``run_in_executor``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from state import ResolvedFact

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_questions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  category      TEXT NOT NULL,
  utterance     TEXT,
  agent_guess   TEXT NOT NULL,
  why_unsure    TEXT NOT NULL,
  pose_json     TEXT NOT NULL,
  excerpt_json  TEXT NOT NULL,
  frame_path    TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',
  resolved_ts   TEXT,
  resolution    TEXT,
  dismiss_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON pending_questions(status);

CREATE TABLE IF NOT EXISTS resolved_knowledge (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  question_id   INTEGER REFERENCES pending_questions(id),
  ts            TEXT NOT NULL,
  category      TEXT NOT NULL,
  topic         TEXT NOT NULL,
  resolution    TEXT NOT NULL,
  evicted       INTEGER NOT NULL DEFAULT 0
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class QueueDB:
    def __init__(self, db_path: str | Path, frames_dir: str | Path) -> None:
        self.db_path = Path(db_path)
        self.frames_dir = Path(frames_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- write path (agent) ---------------------------------------------------
    def queue_question(
        self,
        category: str,
        utterance: Optional[str],
        agent_guess: str,
        why_unsure: str,
        pose: Optional[dict] = None,
        excerpt: Optional[list] = None,
        frame_jpeg: Optional[bytes] = None,
    ) -> int:
        """Insert a pending question, optionally saving the camera frame. Returns id."""
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO pending_questions
                   (ts, category, utterance, agent_guess, why_unsure, pose_json, excerpt_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    _now(),
                    category,
                    utterance,
                    agent_guess,
                    why_unsure,
                    json.dumps(pose or {}),
                    json.dumps(excerpt or []),
                ),
            )
            qid = int(cur.lastrowid)
            frame_path: Optional[str] = None
            if frame_jpeg:
                fp = self.frames_dir / f"{qid}.jpg"
                fp.write_bytes(frame_jpeg)
                frame_path = fp.name  # relative to frames_dir
                self._conn.execute(
                    "UPDATE pending_questions SET frame_path=? WHERE id=?", (frame_path, qid)
                )
            self._conn.commit()
            return qid

    # --- read path ------------------------------------------------------------
    def list_pending(self, status_filter: str = "pending", limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, ts, category, utterance, agent_guess, status
                   FROM pending_questions WHERE status=? ORDER BY id DESC LIMIT ?""",
                (status_filter, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def next_pending(self) -> Optional[dict[str, Any]]:
        """Oldest still-pending question as a full record (like :meth:`get_question`),
        or None. Lets the human's Claude triage one-at-a-time without listing first."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM pending_questions WHERE status='pending' ORDER BY id ASC LIMIT 1"
            ).fetchone()
        return self.get_question(int(row["id"])) if row else None

    def get_question(self, qid: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_questions WHERE id=?", (qid,)
            ).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["pose"] = json.loads(rec.pop("pose_json") or "{}")
        rec["excerpt"] = json.loads(rec.pop("excerpt_json") or "[]")
        if rec.get("frame_path"):
            rec["frame_abspath"] = str(self.frames_dir / rec["frame_path"])
        return rec

    def count_pending(self) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM pending_questions WHERE status='pending'"
                ).fetchone()[0]
            )

    def summarize_queue(self) -> str:
        with self._lock:
            rows = self._conn.execute(
                """SELECT category, COUNT(*) n FROM pending_questions
                   WHERE status='pending' GROUP BY category ORDER BY n DESC"""
            ).fetchall()
        if not rows:
            return "No pending questions."
        total = sum(r["n"] for r in rows)
        parts = ", ".join(f"{r['n']} {r['category']}" for r in rows)
        return f"{total} pending question(s): {parts}."

    # --- resolution -----------------------------------------------------------
    def resolve_question(
        self, qid: int, resolution_text: str, share_with_robot: bool = True
    ) -> Optional[ResolvedFact]:
        """Mark resolved. If shared, record knowledge and return the fact to push
        onto the recent-answers buffer; otherwise return None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT category, utterance, agent_guess FROM pending_questions WHERE id=?",
                (qid,),
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE pending_questions SET status='resolved', resolved_ts=?, resolution=? WHERE id=?",
                (_now(), resolution_text, qid),
            )
            fact: Optional[ResolvedFact] = None
            if share_with_robot:
                topic = (row["utterance"] or row["agent_guess"] or "").strip()[:80]
                self._conn.execute(
                    """INSERT INTO resolved_knowledge (question_id, ts, category, topic, resolution)
                       VALUES (?,?,?,?,?)""",
                    (qid, _now(), row["category"], topic, resolution_text),
                )
                fact = ResolvedFact(category=row["category"], topic=topic, resolution=resolution_text)
            self._conn.commit()
            return fact

    def dismiss_question(self, qid: int, reason: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE pending_questions SET status='dismissed', dismiss_reason=? WHERE id=?",
                (reason, qid),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # --- startup seeding ------------------------------------------------------
    def load_recent_resolutions(self, limit: int = 50) -> list[ResolvedFact]:
        """Most-recent `limit` shared resolutions, oldest first (for the buffer)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT category, topic, resolution FROM resolved_knowledge ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        facts = [ResolvedFact(r["category"], r["topic"], r["resolution"]) for r in rows]
        facts.reverse()
        return facts
