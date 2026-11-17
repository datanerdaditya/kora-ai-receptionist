"""Booking service: every rule about appointments lives here.

The agent never touches storage directly. It calls BookingService, which
checks the clinic's rules and then asks a backend to store the result.
The demo backend is a local SQLite file. A Google Calendar or
practice-software backend can replace it by implementing BookingBackend.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Protocol

from clinic_config import DAYS, ClinicConfig, Service

UTC = timezone.utc
_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I


class BookingError(Exception):
    """A problem that is safe to explain to the patient."""


class SlotTakenError(BookingError):
    pass


@dataclass(frozen=True)
class Appointment:
    id: str
    clinic_id: str
    service: str
    practitioner: str
    start: datetime  # timezone-aware, UTC
    end: datetime  # timezone-aware, UTC
    patient_name: str
    patient_phone: str
    status: str = "booked"  # "booked" or "cancelled"


def _to_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _from_text(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def normalise_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0033"):
        digits = "0" + digits[4:]
    elif digits.startswith("33") and len(digits) == 11:
        digits = "0" + digits[2:]
    if not 8 <= len(digits) <= 15:
        raise BookingError(
            "That phone number does not look complete. Ask the caller to repeat it."
        )
    return digits


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


class BookingBackend(Protocol):
    def booked_between(
        self, clinic_id: str, start: datetime, end: datetime
    ) -> list[Appointment]: ...

    def create(self, appointment: Appointment, buffer: timedelta) -> None:
        """Store the appointment, or raise SlotTakenError if the practitioner is busy."""

    def move(
        self,
        appointment_id: str,
        practitioner: str,
        start: datetime,
        end: datetime,
        buffer: timedelta,
    ) -> None:
        """Move the appointment, or raise SlotTakenError if the practitioner is busy."""

    def cancel(self, appointment_id: str) -> None: ...

    def get(self, appointment_id: str) -> Appointment | None: ...

    def upcoming_for_phone(
        self, clinic_id: str, phone: str, after: datetime
    ) -> list[Appointment]: ...

    def delete_patient(self, clinic_id: str, phone: str) -> int: ...


class SQLiteBackend:
    """Stores appointments in one local file. Safe across several processes."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS appointments (
                    id TEXT PRIMARY KEY,
                    clinic_id TEXT NOT NULL,
                    service TEXT NOT NULL,
                    practitioner TEXT NOT NULL,
                    start_utc TEXT NOT NULL,
                    end_utc TEXT NOT NULL,
                    patient_name TEXT NOT NULL,
                    patient_phone TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_utc TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_clinic_start "
                "ON appointments (clinic_id, start_utc)"
            )

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: we open and close transactions ourselves
        conn = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row(row: sqlite3.Row) -> Appointment:
        return Appointment(
            id=row["id"],
            clinic_id=row["clinic_id"],
            service=row["service"],
            practitioner=row["practitioner"],
            start=_from_text(row["start_utc"]),
            end=_from_text(row["end_utc"]),
            patient_name=row["patient_name"],
            patient_phone=row["patient_phone"],
            status=row["status"],
        )

    @staticmethod
    def _is_busy(
        conn: sqlite3.Connection,
        clinic_id: str,
        practitioner: str,
        start: datetime,
        end: datetime,
        buffer: timedelta,
        ignore_id: str = "",
    ) -> bool:
        row = conn.execute(
            """
            SELECT 1 FROM appointments
            WHERE clinic_id = ? AND practitioner = ? AND status = 'booked'
              AND id != ? AND start_utc < ? AND end_utc > ?
            LIMIT 1
            """,
            (
                clinic_id,
                practitioner,
                ignore_id,
                _to_text(end + buffer),
                _to_text(start - buffer),
            ),
        ).fetchone()
        return row is not None

    def booked_between(
        self, clinic_id: str, start: datetime, end: datetime
    ) -> list[Appointment]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM appointments
                WHERE clinic_id = ? AND status = 'booked'
                  AND start_utc < ? AND end_utc > ?
                ORDER BY start_utc
                """,
                (clinic_id, _to_text(end), _to_text(start)),
            ).fetchall()
        return [self._row(row) for row in rows]

    def create(self, appointment: Appointment, buffer: timedelta) -> None:
        conn = self._connect()
        try:
            # IMMEDIATE takes the write lock first, so two calls booking the
            # same slot at the same moment cannot both pass the check below.
            conn.execute("BEGIN IMMEDIATE")
            if self._is_busy(
                conn,
                appointment.clinic_id,
                appointment.practitioner,
                appointment.start,
                appointment.end,
                buffer,
            ):
                conn.execute("ROLLBACK")
                raise SlotTakenError("That time has just been taken.")
            conn.execute(
                "INSERT INTO appointments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    appointment.id,
                    appointment.clinic_id,
                    appointment.service,
                    appointment.practitioner,
                    _to_text(appointment.start),
                    _to_text(appointment.end),
                    appointment.patient_name,
                    appointment.patient_phone,
                    appointment.status,
                    _to_text(datetime.now(UTC)),
                ),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def move(
        self,
        appointment_id: str,
        practitioner: str,
        start: datetime,
        end: datetime,
        buffer: timedelta,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT clinic_id FROM appointments WHERE id = ? AND status = 'booked'",
                (appointment_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise BookingError("That appointment no longer exists.")
            if self._is_busy(
                conn, row["clinic_id"], practitioner, start, end, buffer, appointment_id
            ):
                conn.execute("ROLLBACK")
                raise SlotTakenError("That time has just been taken.")
            conn.execute(
                "UPDATE appointments SET practitioner = ?, start_utc = ?, end_utc = ? "
                "WHERE id = ?",
                (practitioner, _to_text(start), _to_text(end), appointment_id),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def cancel(self, appointment_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE appointments SET status = 'cancelled' WHERE id = ?",
                (appointment_id,),
            )

    def get(self, appointment_id: str) -> Appointment | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
            ).fetchone()
        return self._row(row) if row else None

    def upcoming_for_phone(
        self, clinic_id: str, phone: str, after: datetime
    ) -> list[Appointment]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM appointments
                WHERE clinic_id = ? AND patient_phone = ? AND status = 'booked'
                  AND end_utc > ?
                ORDER BY start_utc
                """,
                (clinic_id, phone, _to_text(after)),
            ).fetchall()
        return [self._row(row) for row in rows]

    def delete_patient(self, clinic_id: str, phone: str) -> int:
        """Erase everything stored about one patient. Returns rows deleted."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM appointments WHERE clinic_id = ? AND patient_phone = ?",
                (clinic_id, phone),
            )
            return cursor.rowcount


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    start: datetime  # clinic local time
    practitioners: tuple[str, ...]  # who is free at that time


class BookingService:
    def __init__(self, clinic: ClinicConfig, backend: BookingBackend) -> None:
        self.clinic = clinic
        self.backend = backend
        self._buffer = timedelta(minutes=clinic.booking.buffer_minutes)

    # ----- matching what the caller said to the config -----

    def bookable_services(self) -> list[Service]:
        return [s for s in self.clinic.services if s.practitioners]

    def resolve_service(self, name: str) -> Service:
        wanted = (name or "").strip().lower()
        services = self.bookable_services()
        if wanted:
            for service in services:
                if service.name.lower() == wanted:
                    return service
            partial = [
                s for s in services if wanted in s.name.lower() or s.name.lower() in wanted
            ]
            if len(partial) == 1:
                return partial[0]
            # "check-up" matches two services; prefer the one that starts with it
            leading = [s for s in partial if s.name.lower().startswith(wanted)]
            if len(leading) == 1:
                return leading[0]
        options = ", ".join(s.name for s in services)
        raise BookingError(f"Unknown service '{name}'. Bookable services are: {options}.")

    def resolve_practitioner(self, name: str | None, service: Service) -> str | None:
        wanted = (name or "").strip().lower()
        if not wanted or wanted in {"any", "anyone", "no preference", "none"}:
            return None
        wanted = re.sub(r"^(dr\.?|doctor)\s+", "", wanted)
        matches = [p for p in service.practitioners if wanted in p.lower()]
        if len(matches) == 1:
            return matches[0]
        options = ", ".join(service.practitioners)
        raise BookingError(
            f"'{name}' does not do {service.name}. It is done by: {options}."
        )

    # ----- time rules -----

    def parse_day(self, text: str) -> date:
        try:
            return date.fromisoformat((text or "").strip())
        except ValueError:
            raise BookingError(
                f"'{text}' is not a valid date. Use the format YYYY-MM-DD."
            ) from None

    def parse_start(self, day_text: str, time_text: str) -> datetime:
        day = self.parse_day(day_text)
        try:
            clock = time.fromisoformat((time_text or "").strip())
        except ValueError:
            raise BookingError(
                f"'{time_text}' is not a valid time. Use the 24-hour format HH:MM."
            ) from None
        return datetime.combine(day, clock.replace(second=0, microsecond=0), self.clinic.tz)

    def opening_hours(self, day: date) -> tuple[datetime, datetime] | None:
        """Opening and closing time on that day, or None if the clinic is closed."""
        if any(closed.date == day for closed in self.clinic.closed_dates):
            return None
        hours = self.clinic.hours[DAYS[day.weekday()]]
        if hours == "closed":
            return None
        opens, closes = (time.fromisoformat(part) for part in hours.split("-"))
        tz = self.clinic.tz
        return datetime.combine(day, opens, tz), datetime.combine(day, closes, tz)

    def _check_start(self, service: Service, start: datetime, now: datetime) -> datetime:
        """Raise BookingError if the clinic's rules forbid this start. Returns the end."""
        rules = self.clinic.booking
        end = start + timedelta(minutes=service.duration_minutes)
        hours = self.opening_hours(start.date())
        if hours is None:
            raise BookingError("The clinic is closed on that day.")
        opens, closes = hours
        if start < opens or end > closes:
            raise BookingError(
                f"That is outside opening hours. On that day the clinic is open "
                f"{opens:%H:%M} to {closes:%H:%M}, and {service.name} takes "
                f"{service.duration_minutes} minutes."
            )
        notice = (
            service.min_notice_minutes
            if service.min_notice_minutes is not None
            else rules.min_notice_minutes
        )
        if start < now + timedelta(minutes=notice):
            raise BookingError(
                f"That is too soon. {service.name} needs at least {notice} minutes' notice."
            )
        if start.date() > now.date() + timedelta(days=rules.max_days_ahead):
            raise BookingError(
                f"That is too far ahead. Appointments can be booked up to "
                f"{rules.max_days_ahead} days in advance."
            )
        minutes_from_opening = int((start - opens).total_seconds() // 60)
        if minutes_from_opening % rules.slot_minutes != 0:
            raise BookingError(
                f"Appointments start every {rules.slot_minutes} minutes from opening time. "
                "Check availability to get valid start times."
            )
        return end

    def _is_free(
        self,
        booked: list[Appointment],
        practitioner: str,
        start: datetime,
        end: datetime,
        ignore_id: str = "",
    ) -> bool:
        for appointment in booked:
            if appointment.practitioner != practitioner or appointment.id == ignore_id:
                continue
            if start < appointment.end + self._buffer and end > appointment.start - self._buffer:
                return False
        return True

    # ----- availability -----

    def free_slots(
        self,
        service: Service,
        day: date,
        practitioner: str | None = None,
        now: datetime | None = None,
        ignore_id: str = "",
    ) -> list[Slot]:
        now = now or self.clinic.now()
        hours = self.opening_hours(day)
        if hours is None:
            return []
        opens, closes = hours
        candidates = [practitioner] if practitioner else list(service.practitioners)
        booked = self.backend.booked_between(self.clinic.id, opens, closes)
        step = timedelta(minutes=self.clinic.booking.slot_minutes)
        slots: list[Slot] = []
        start = opens
        while start + timedelta(minutes=service.duration_minutes) <= closes:
            try:
                end = self._check_start(service, start, now)
            except BookingError:
                start += step
                continue
            free = tuple(
                name for name in candidates if self._is_free(booked, name, start, end, ignore_id)
            )
            if free:
                slots.append(Slot(start=start, practitioners=free))
            start += step
        return slots

    def next_day_with_slots(
        self,
        service: Service,
        after: date,
        practitioner: str | None = None,
        now: datetime | None = None,
        search_days: int = 21,
    ) -> tuple[date, list[Slot]] | None:
        for offset in range(1, search_days + 1):
            day = after + timedelta(days=offset)
            slots = self.free_slots(service, day, practitioner, now)
            if slots:
                return day, slots
        return None

    # ----- actions -----

    def book(
        self,
        service_name: str,
        day_text: str,
        time_text: str,
        patient_name: str,
        patient_phone: str,
        practitioner_name: str | None = None,
        now: datetime | None = None,
    ) -> Appointment:
        now = now or self.clinic.now()
        service = self.resolve_service(service_name)
        wanted = self.resolve_practitioner(practitioner_name, service)
        name = " ".join((patient_name or "").split())
        if len(name) < 2:
            raise BookingError("The caller's full name is needed to book.")
        phone = normalise_phone(patient_phone)
        start = self.parse_start(day_text, time_text)
        end = self._check_start(service, start, now)

        # If the same request arrives twice, return the first booking, not a second one
        for existing in self.backend.upcoming_for_phone(self.clinic.id, phone, now):
            if existing.service == service.name and existing.start == start:
                return existing

        candidates = [wanted] if wanted else list(service.practitioners)
        for practitioner in candidates:
            appointment = Appointment(
                id="".join(secrets.choice(_ID_ALPHABET) for _ in range(6)),
                clinic_id=self.clinic.id,
                service=service.name,
                practitioner=practitioner,
                start=start.astimezone(UTC),
                end=end.astimezone(UTC),
                patient_name=name,
                patient_phone=phone,
            )
            try:
                self.backend.create(appointment, self._buffer)
                return appointment
            except SlotTakenError:
                continue
        raise SlotTakenError(
            "That time is no longer free. Check availability again and offer another time."
        )

    def find(self, patient_phone: str, now: datetime | None = None) -> list[Appointment]:
        now = now or self.clinic.now()
        return self.backend.upcoming_for_phone(
            self.clinic.id, normalise_phone(patient_phone), now
        )

    def _owned(self, appointment_id: str, patient_phone: str) -> Appointment:
        """The caller may only change an appointment booked with their phone number."""
        phone = normalise_phone(patient_phone)
        appointment = self.backend.get((appointment_id or "").strip().upper())
        if (
            appointment is None
            or appointment.clinic_id != self.clinic.id
            or appointment.patient_phone != phone
            or appointment.status != "booked"
        ):
            raise BookingError(
                "No booked appointment matches that reference and phone number. "
                "Look up the caller's appointments first."
            )
        return appointment

    def reschedule(
        self,
        appointment_id: str,
        patient_phone: str,
        new_day_text: str,
        new_time_text: str,
        now: datetime | None = None,
    ) -> Appointment:
        now = now or self.clinic.now()
        appointment = self._owned(appointment_id, patient_phone)
        if appointment.end <= now:
            raise BookingError("That appointment is already in the past.")
        service = self.resolve_service(appointment.service)
        start = self.parse_start(new_day_text, new_time_text)
        end = self._check_start(service, start, now)
        # Keep the same practitioner if possible, otherwise anyone who does this service
        candidates = [appointment.practitioner] + [
            name for name in service.practitioners if name != appointment.practitioner
        ]
        for practitioner in candidates:
            try:
                self.backend.move(appointment.id, practitioner, start, end, self._buffer)
            except SlotTakenError:
                continue
            return replace(
                appointment,
                practitioner=practitioner,
                start=start.astimezone(UTC),
                end=end.astimezone(UTC),
            )
        raise SlotTakenError(
            "That time is not free. Check availability again and offer another time."
        )

    def cancel(
        self, appointment_id: str, patient_phone: str, now: datetime | None = None
    ) -> Appointment:
        now = now or self.clinic.now()
        appointment = self._owned(appointment_id, patient_phone)
        if appointment.end <= now:
            raise BookingError("That appointment is already in the past.")
        self.backend.cancel(appointment.id)
        return replace(appointment, status="cancelled")

    def delete_patient_data(self, patient_phone: str) -> int:
        return self.backend.delete_patient(self.clinic.id, normalise_phone(patient_phone))