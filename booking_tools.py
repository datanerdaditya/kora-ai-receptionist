"""Turns booking results into short plain text for the LLM.

These are ordinary functions with no voice-framework code, so they can be
tested on their own. agent.py exposes them to Claude as tools.
Every function returns a string and never raises: a failure comes back as
text that tells the model what went wrong and what to do next.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from booking_service import Appointment, BookingError, BookingService, Slot

logger = logging.getLogger("clinic-agent.booking")

SYSTEM_PROBLEM = (
    "FAILED: the booking system had a technical problem and nothing was changed. "
    "Apologise and give the clinic's phone number."
)


def _day_text(moment: datetime) -> str:
    return f"{moment:%A} {moment.day} {moment:%B %Y}"


class BookingTools:
    def __init__(self, service: BookingService) -> None:
        self.service = service
        self.clinic = service.clinic

    def _local(self, moment: datetime) -> datetime:
        return moment.astimezone(self.clinic.tz)

    def _describe(self, appointment: Appointment) -> str:
        start = self._local(appointment.start)
        return (
            f"{appointment.service} with {appointment.practitioner} on "
            f"{_day_text(start)} at {start:%H:%M} (reference {appointment.id})"
        )

    def _is_late(self, appointment: Appointment) -> bool:
        window = timedelta(hours=self.clinic.booking.late_cancellation_hours)
        return appointment.start - self.clinic.now() < window

    def _late_note(self) -> str:
        hours = self.clinic.booking.late_cancellation_hours
        return (
            f"less than {hours} hours away, so cancelling or moving it now is late: before "
            "asking the caller to confirm, tell them what the clinic's policies say about this"
        )

    def _times(self, slots: list[Slot]) -> str:
        return ", ".join(f"{slot.start:%H:%M}" for slot in slots)

    def _run(self, action: str, work: Callable[[], str]) -> str:
        try:
            result = work()
            logger.info("tool %s ok", action)
            return result
        except BookingError as error:
            logger.info("tool %s refused: %s", action, type(error).__name__)
            return f"FAILED: {error} Nothing was changed."
        except Exception:
            logger.exception("tool %s crashed", action)
            return SYSTEM_PROBLEM

    # ----- tools -----

    def check_availability(
        self, service: str, date: str, practitioner: str | None = None
    ) -> str:
        def work() -> str:
            chosen = self.service.resolve_service(service)
            who = self.service.resolve_practitioner(practitioner, chosen)
            day = self.service.parse_day(date)
            with_whom = f" with {who}" if who else ""
            header = f"{chosen.name}{with_whom}, {chosen.duration_minutes} minutes."
            slots = self.service.free_slots(chosen, day, who)
            day_label = _day_text(datetime.combine(day, datetime.min.time()))
            if slots:
                return (
                    f"{header} Free start times on {day_label} (24-hour clock): "
                    f"{self._times(slots)}. Offer the caller two or three of these."
                )
            following = self.service.next_day_with_slots(chosen, day, who)
            if following is None:
                return (
                    f"{header} No free times on {day_label} or in the three weeks after. "
                    "Suggest calling the clinic."
                )
            next_day, next_slots = following
            next_label = _day_text(datetime.combine(next_day, datetime.min.time()))
            return (
                f"{header} No free times on {day_label}. The next day with free times "
                f"is {next_label}: {self._times(next_slots)}."
            )

        return self._run("check_availability", work)

    def book_appointment(
        self,
        service: str,
        date: str,
        time: str,
        patient_name: str,
        patient_phone: str,
        practitioner: str | None = None,
    ) -> str:
        def work() -> str:
            appointment = self.service.book(
                service, date, time, patient_name, patient_phone, practitioner
            )
            return (
                f"BOOKED: {self._describe(appointment)} for {appointment.patient_name}. "
                "Confirm these details to the caller."
            )

        return self._run("book_appointment", work)

    def find_appointments(self, patient_phone: str) -> str:
        def work() -> str:
            found = self.service.find(patient_phone)
            if not found:
                return (
                    "No upcoming appointments are booked with that phone number. "
                    "Check the number with the caller."
                )
            lines = "; ".join(
                f"{self._describe(a)} for {a.patient_name}"
                + (f" ({self._late_note()})" if self._is_late(a) else "")
                for a in found
            )
            return f"Upcoming appointments for that phone number: {lines}."

        return self._run("find_appointments", work)

    def reschedule_appointment(
        self, appointment_reference: str, patient_phone: str, new_date: str, new_time: str
    ) -> str:
        def work() -> str:
            moved = self.service.reschedule(
                appointment_reference, patient_phone, new_date, new_time
            )
            return (
                f"RESCHEDULED: now {self._describe(moved)}. "
                "Confirm the new details to the caller."
            )

        return self._run("reschedule_appointment", work)

    def cancel_appointment(self, appointment_reference: str, patient_phone: str) -> str:
        def work() -> str:
            cancelled = self.service.cancel(appointment_reference, patient_phone)
            text = f"CANCELLED: {self._describe(cancelled)}. Confirm this to the caller."
            if self._is_late(cancelled):
                text += (
                    " This was a late cancellation. If you have not yet told the caller "
                    "what the clinic's policies say about this, do so now."
                )
            return text

        return self._run("cancel_appointment", work)