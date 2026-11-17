"""Callback requests: how the agent hands a caller over to clinic staff.

The agent cannot transfer a call. Instead it saves a request that staff see
(in the terminal with staff.py for now, on the admin page from Phase 5).
Stored in the same local database file as appointments.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from booking_service import BookingError, normalise_phone
from clinic_config import ClinicConfig

logger = logging.getLogger("clinic-agent.callback")
UTC = timezone.utc
_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MAX_REASON_CHARS = 500


@dataclass(frozen=True)
class CallbackRequest:
    id: str
    clinic_id: str
    created: datetime  # timezone-aware, UTC
    patient_name: str
    patient_phone: str
    reason: str
    urgent: bool
    status: str  # "open" or "done"


def _to_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _from_text(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


class CallbackService:
    def __init__(self, clinic: ClinicConfig, path: str | Path) -> None:
        self.clinic = clinic
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS callback_requests (
                    id TEXT PRIMARY KEY,
                    clinic_id TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    patient_name TEXT NOT NULL,
                    patient_phone TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    urgent INTEGER NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row(row: sqlite3.Row) -> CallbackRequest:
        return CallbackRequest(
            id=row["id"],
            clinic_id=row["clinic_id"],
            created=_from_text(row["created_utc"]),
            patient_name=row["patient_name"],
            patient_phone=row["patient_phone"],
            reason=row["reason"],
            urgent=bool(row["urgent"]),
            status=row["status"],
        )

    def create(
        self,
        patient_name: str,
        patient_phone: str,
        reason: str,
        urgent: bool = False,
        now: datetime | None = None,
    ) -> CallbackRequest:
        now = now or datetime.now(UTC)
        name = " ".join((patient_name or "").split())
        if len(name) < 2:
            raise BookingError("The caller's name is needed to leave a callback request.")
        phone = normalise_phone(patient_phone)
        text = " ".join((reason or "").split())
        if len(text) < 3:
            raise BookingError("Say briefly what the caller needs, so staff can prepare.")
        text = text[:MAX_REASON_CHARS]

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # One open request per caller in a short window: a repeat updates the
            # first one, so staff do not get the same person twice.
            recent = conn.execute(
                """
                SELECT * FROM callback_requests
                WHERE clinic_id = ? AND patient_phone = ? AND status = 'open'
                  AND created_utc > ?
                ORDER BY created_utc DESC LIMIT 1
                """,
                (self.clinic.id, phone, _to_text(now - timedelta(minutes=30))),
            ).fetchone()
            if recent is not None:
                merged_urgent = bool(recent["urgent"]) or urgent
                conn.execute(
                    "UPDATE callback_requests SET patient_name = ?, reason = ?, urgent = ? "
                    "WHERE id = ?",
                    (name, text, int(merged_urgent), recent["id"]),
                )
                request_id, created = recent["id"], _from_text(recent["created_utc"])
                urgent = merged_urgent
            else:
                request_id = "".join(secrets.choice(_ID_ALPHABET) for _ in range(6))
                created = now
                conn.execute(
                    "INSERT INTO callback_requests VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
                    (request_id, self.clinic.id, _to_text(now), name, phone, text, int(urgent)),
                )
            conn.execute("COMMIT")
        finally:
            conn.close()
        return CallbackRequest(
            request_id, self.clinic.id, created, name, phone, text, urgent, "open"
        )

    def open_requests(self) -> list[CallbackRequest]:
        """Urgent first, then oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM callback_requests
                WHERE clinic_id = ? AND status = 'open'
                ORDER BY urgent DESC, created_utc ASC
                """,
                (self.clinic.id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def mark_done(self, request_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE callback_requests SET status = 'done' "
                "WHERE id = ? AND clinic_id = ? AND status = 'open'",
                ((request_id or "").strip().upper(), self.clinic.id),
            )
            return cursor.rowcount == 1

    def delete_patient(self, patient_phone: str) -> int:
        """Erase every callback request for one patient. Returns rows deleted."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM callback_requests WHERE clinic_id = ? AND patient_phone = ?",
                (self.clinic.id, normalise_phone(patient_phone)),
            )
            return cursor.rowcount


def request_callback_text(
    service: CallbackService, patient_name: str, patient_phone: str, reason: str, urgent: bool
) -> str:
    """What the agent's tool returns. Always text, never an exception."""
    try:
        saved = service.create(patient_name, patient_phone, reason, bool(urgent))
    except BookingError as error:
        logger.info("tool request_callback refused")
        return f"FAILED: {error} Nothing was saved."
    except Exception:
        logger.exception("tool request_callback crashed")
        return (
            "FAILED: the callback request could not be saved. "
            "Apologise and give the clinic's phone number."
        )
    logger.info("tool request_callback ok urgent=%s", saved.urgent)
    kind = "URGENT callback request" if saved.urgent else "Callback request"
    return (
        f"SAVED: {kind} for {saved.patient_name}. Tell the caller that staff will call "
        "them back during opening hours. Do not promise a time."
    )