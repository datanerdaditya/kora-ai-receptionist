"""Shows what staff need to see: callback requests and upcoming appointments.

A stand-in until the admin page exists (Phase 5).

    uv run python staff.py                    show everything
    uv run python staff.py done ABC123        mark a callback request as handled
    uv run python staff.py forget 0612345678  erase one patient's data
"""

import os
import sys
from datetime import timedelta

from booking_service import BookingService, SQLiteBackend
from callback_service import CallbackService
from clinic_config import load_clinic

CLINIC_ID = os.getenv("CLINIC_ID", "riviere-dental")
DB = os.getenv("BOOKINGS_DB", "data/bookings.db")


def main() -> None:
    clinic = load_clinic(CLINIC_ID)
    bookings = BookingService(clinic, SQLiteBackend(DB))
    callbacks = CallbackService(clinic, DB)
    args = sys.argv[1:]

    if len(args) == 2 and args[0] == "done":
        print("Marked as handled." if callbacks.mark_done(args[1]) else "No open request with that reference.")
        return
    if len(args) == 2 and args[0] == "forget":
        removed = bookings.delete_patient_data(args[1]) + callbacks.delete_patient(args[1])
        print(f"Erased {removed} record(s) for that phone number.")
        return
    if args:
        print(__doc__)
        return

    print(f"\n{clinic.name}\n")
    waiting = callbacks.open_requests()
    print(f"CALLBACK REQUESTS ({len(waiting)} open)")
    for request in waiting:
        when = request.created.astimezone(clinic.tz)
        flag = "URGENT " if request.urgent else ""
        print(f"  {flag}[{request.id}] {when:%a %d %b %H:%M}  {request.patient_name}, {request.patient_phone}")
        print(f"      {request.reason}")
    if not waiting:
        print("  none")

    now = clinic.now()
    upcoming = bookings.backend.booked_between(clinic.id, now, now + timedelta(days=90))
    print(f"\nUPCOMING APPOINTMENTS ({len(upcoming)})")
    for appointment in upcoming:
        start = appointment.start.astimezone(clinic.tz)
        print(
            f"  [{appointment.id}] {start:%a %d %b %H:%M}  {appointment.service} with "
            f"{appointment.practitioner}: {appointment.patient_name}, {appointment.patient_phone}"
        )
    if not upcoming:
        print("  none")
    print()


if __name__ == "__main__":
    main()