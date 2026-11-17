"""Call transcripts for clinic staff.

Kept in the same local database as appointments. Transcripts hold personal
details, so they are deleted automatically after the clinic's retention period
and can be deleted by hand at any time.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from clinic_config import ClinicConfig

UTC = timezone.utc
MAX_TEXT_CHARS = 2000  # per line of the transcript


@dataclass(frozen=True)
class CallRecord:
    id: str
    clinic_id: str
    started: datetime  # timezone-aware, UTC
    ended: datetime
    lines: list[dict[str, str]]  # {"who": "caller" | "assistant" | "action", "text": ...}


def _to_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _from_text(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def lines_from_history(items: list[Any]) -> list[dict[str, str]]:
    """Turns the voice framework's conversation history into plain transcript lines.

    Tool arguments are left out; only the tool's name and its result are kept,
    so staff can see what the assistant actually did.
    """
    lines: list[dict[str, str]] = []
    for item in items:
        kind = getattr(item, "type", "")
        if kind == "message":
            role = getattr(item, "role", "")
            text = (getattr(item, "text_content", None) or "").strip()
            if role not in ("user", "assistant") or not text:
                continue
            lines.append(
                {"who": "caller" if role == "user" else "assistant", "text": text[:MAX_TEXT_CHARS]}
            )
        elif kind == "function_call_output":
            name = getattr(item, "name", "tool")
            output = str(getattr(item, "output", "") or "").strip()
            if name == "check_availability":
                output = "checked availability"  # the full list of times is noise
            lines.append({"who": "action", "text": f"{name}: {output}"[:MAX_TEXT_CHARS]})
    return lines


class CallLog:
    def __init__(self, clinic: ClinicConfig, path: str | Path) -> None:
        self.clinic = clinic
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY,
                    clinic_id TEXT NOT NULL,
                    started_utc TEXT NOT NULL,
                    ended_utc TEXT NOT NULL,
                    lines_json TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row(row: sqlite3.Row) -> CallRecord:
        return CallRecord(
            id=row["id"],
            clinic_id=row["clinic_id"],
            started=_from_text(row["started_utc"]),
            ended=_from_text(row["ended_utc"]),
            lines=json.loads(row["lines_json"]),
        )

    def save(
        self, started: datetime, lines: list[dict[str, str]], ended: datetime | None = None
    ) -> str | None:
        """Stores one call. Returns its id, or None if nothing was stored."""
        self.purge_old()
        if self.clinic.transcript_retention_days == 0:
            return None  # this clinic does not keep transcripts
        if not any(line["who"] == "caller" for line in lines):
            return None  # the caller never said anything: nothing worth keeping
        call_id = secrets.token_hex(6)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO calls VALUES (?, ?, ?, ?, ?)",
                (
                    call_id,
                    self.clinic.id,
                    _to_text(started),
                    _to_text(ended or datetime.now(UTC)),
                    json.dumps(lines, ensure_ascii=False),
                ),
            )
        return call_id

    def purge_old(self, now: datetime | None = None) -> int:
        cutoff = (now or datetime.now(UTC)) - timedelta(
            days=self.clinic.transcript_retention_days
        )
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM calls WHERE clinic_id = ? AND ended_utc < ?",
                (self.clinic.id, _to_text(cutoff)),
            )
            return cursor.rowcount

    def recent(self, limit: int = 50) -> list[CallRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM calls WHERE clinic_id = ? ORDER BY started_utc DESC LIMIT ?",
                (self.clinic.id, limit),
            ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, call_id: str) -> CallRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM calls WHERE id = ? AND clinic_id = ?", (call_id, self.clinic.id)
            ).fetchone()
        return self._row(row) if row else None

    def delete(self, call_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM calls WHERE id = ? AND clinic_id = ?", (call_id, self.clinic.id)
            )
            return cursor.rowcount == 1

    def delete_mentioning(self, phone_digits: str) -> int:
        """Deletes every transcript in which this phone number appears, however it was spaced."""
        removed = 0
        for record in self.recent(limit=100000):
            text = " ".join(line["text"] for line in record.lines)
            if phone_digits and phone_digits in "".join(ch for ch in text if ch.isdigit()):
                removed += int(self.delete(record.id))
        return removed