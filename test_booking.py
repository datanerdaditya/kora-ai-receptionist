"""Checks the booking rules without any voice or LLM.

Run with:  uv run python test_booking.py
Uses a temporary database, so it never touches real bookings.
"""

import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

from booking_service import BookingError, BookingService, SlotTakenError, SQLiteBackend
from booking_tools import BookingTools
from callback_service import CallbackService, request_callback_text
from clinic_config import load_clinic

clinic = load_clinic("riviere-dental")
TZ = clinic.tz
# A fixed "now" so the tests give the same result every day: Monday 16 Nov 2026, 10:00
NOW = datetime(2026, 11, 16, 10, 0, tzinfo=TZ)
CLEANING = "Scale and polish (cleaning)"
CHECKUP = "Check-up and consultation"


def fresh() -> BookingService:
    folder = tempfile.mkdtemp()
    return BookingService(clinic, SQLiteBackend(Path(folder) / "test.db"))


def refuses(action, text: str) -> None:
    try:
        action()
    except BookingError as error:
        assert text in str(error), f"expected '{text}' in '{error}'"
        return
    raise AssertionError(f"expected a refusal containing '{text}'")


def test_books_and_blocks_double_booking():
    s = fresh()
    first = s.book("cleaning", "2026-11-17", "09:00", "Ana Roy", "06 12 34 56 78", now=NOW)
    assert first.practitioner == "Sophie Bernard"
    assert first.patient_phone == "0612345678"
    # Only one hygienist, so the same time must be refused for someone else
    refuses(
        lambda: s.book("cleaning", "2026-11-17", "09:00", "Bo Li", "0611111111", now=NOW),
        "no longer free",
    )
    # 45 min + 5 min buffer: 09:45 is blocked, 10:00 is the first free start after
    times = [f"{slot.start:%H:%M}" for slot in s.free_slots(s.resolve_service(CLEANING), first.start.astimezone(TZ).date(), now=NOW)]
    assert "09:45" not in times and "09:30" not in times and "10:00" in times, times


def test_second_practitioner_takes_overlap():
    s = fresh()
    a = s.book(CHECKUP, "2026-11-17", "09:00", "Ana Roy", "0612345678", now=NOW)
    b = s.book(CHECKUP, "2026-11-17", "09:00", "Bo Li", "0611111111", now=NOW)
    assert {a.practitioner, b.practitioner} == {"Dr Claire Moreau", "Dr Julien Lefèvre"}
    refuses(
        lambda: s.book(CHECKUP, "2026-11-17", "09:00", "Cy Mo", "0622222222", now=NOW),
        "no longer free",
    )


def test_same_request_twice_is_one_booking():
    s = fresh()
    a = s.book(CHECKUP, "2026-11-17", "09:00", "Ana Roy", "0612345678", now=NOW)
    b = s.book(CHECKUP, "2026-11-17", "09:00", "Ana Roy", "+33 6 12 34 56 78", now=NOW)
    assert a.id == b.id
    assert len(s.find("0612345678", now=NOW)) == 1


def test_opening_hours_and_closed_days():
    s = fresh()
    refuses(lambda: s.book(CHECKUP, "2026-11-22", "10:00", "Ana Roy", "0612345678", now=NOW), "closed")  # Sunday
    refuses(lambda: s.book(CHECKUP, "2026-12-25", "10:00", "Ana Roy", "0612345678", now=NOW), "closed")  # Christmas
    refuses(lambda: s.book(CHECKUP, "2026-11-17", "08:00", "Ana Roy", "0612345678", now=NOW), "outside opening hours")
    # Saturday closes at 13:00; a 45-minute cleaning at 12:30 would run over
    refuses(lambda: s.book("cleaning", "2026-11-21", "12:30", "Ana Roy", "0612345678", now=NOW), "outside opening hours")
    assert s.book("cleaning", "2026-11-21", "12:15", "Ana Roy", "0612345678", now=NOW)


def test_notice_grid_and_horizon():
    s = fresh()
    refuses(lambda: s.book(CHECKUP, "2026-11-16", "10:30", "Ana Roy", "0612345678", now=NOW), "too soon")
    refuses(lambda: s.book(CHECKUP, "2026-11-15", "10:00", "Ana Roy", "0612345678", now=NOW), "closed")  # yesterday was Sunday
    refuses(lambda: s.book(CHECKUP, "2026-11-14", "10:00", "Ana Roy", "0612345678", now=NOW), "too soon")  # in the past
    refuses(lambda: s.book(CHECKUP, "2026-11-17", "09:10", "Ana Roy", "0612345678", now=NOW), "every 15 minutes")
    refuses(lambda: s.book(CHECKUP, "2027-03-01", "10:00", "Ana Roy", "0612345678", now=NOW), "too far ahead")
    assert s.book(CHECKUP, "2026-11-16", "11:00", "Ana Roy", "0612345678", now=NOW)


def test_bad_input_is_refused():
    s = fresh()
    refuses(lambda: s.book("braces", "2026-11-17", "09:00", "Ana Roy", "0612345678", now=NOW), "Unknown service")
    refuses(lambda: s.book(CHECKUP, "next tuesday", "09:00", "Ana Roy", "0612345678", now=NOW), "not a valid date")
    refuses(lambda: s.book(CHECKUP, "2026-11-17", "9am", "Ana Roy", "0612345678", now=NOW), "not a valid time")
    refuses(lambda: s.book(CHECKUP, "2026-11-17", "09:00", "", "0612345678", now=NOW), "full name")
    refuses(lambda: s.book(CHECKUP, "2026-11-17", "09:00", "Ana Roy", "0612", now=NOW), "phone number")
    refuses(lambda: s.book("cleaning", "2026-11-17", "09:00", "Ana Roy", "0612345678", "Dr Moreau", now=NOW), "does not do")


def test_practitioner_choice():
    s = fresh()
    a = s.book(CHECKUP, "2026-11-17", "09:00", "Ana Roy", "0612345678", "doctor Lefèvre", now=NOW)
    assert a.practitioner == "Dr Julien Lefèvre"
    refuses(
        lambda: s.book(CHECKUP, "2026-11-17", "09:00", "Bo Li", "0611111111", "Lefèvre", now=NOW),
        "no longer free",
    )


def test_reschedule_and_cancel():
    s = fresh()
    a = s.book("cleaning", "2026-11-17", "09:00", "Ana Roy", "0612345678", now=NOW)
    # Someone else's phone number cannot touch it
    refuses(lambda: s.cancel(a.id, "0699999999", now=NOW), "No booked appointment")
    refuses(lambda: s.reschedule(a.id, "0699999999", "2026-11-18", "10:00", now=NOW), "No booked appointment")
    moved = s.reschedule(a.id, "0612345678", "2026-11-18", "10:00", now=NOW)
    assert moved.start.astimezone(TZ) == datetime(2026, 11, 18, 10, 0, tzinfo=TZ)
    # The old time is free again, the new one is taken
    assert s.book("cleaning", "2026-11-17", "09:00", "Bo Li", "0611111111", now=NOW)
    refuses(lambda: s.book("cleaning", "2026-11-18", "10:00", "Cy Mo", "0622222222", now=NOW), "no longer free")
    # Moving onto a taken time is refused and leaves the appointment where it was
    refuses(lambda: s.reschedule(a.id, "0612345678", "2026-11-17", "09:00", now=NOW), "not free")
    assert s.find("0612345678", now=NOW)[0].start == moved.start
    # A small move that overlaps only itself is allowed
    assert s.reschedule(a.id, "0612345678", "2026-11-18", "10:15", now=NOW)
    s.cancel(a.id, "0612345678", now=NOW)
    assert s.find("0612345678", now=NOW) == []
    refuses(lambda: s.cancel(a.id, "0612345678", now=NOW), "No booked appointment")
    assert s.book("cleaning", "2026-11-18", "10:15", "Cy Mo", "0622222222", now=NOW)


def test_clock_change_day():
    # Clocks go forward on Sunday 28 March 2027; Monday 29 March must still work
    s = fresh()
    late = datetime(2027, 3, 26, 10, 0, tzinfo=TZ)
    a = s.book(CHECKUP, "2027-03-29", "09:00", "Ana Roy", "0612345678", now=late)
    assert a.start.astimezone(TZ).hour == 9 and a.start.hour == 7  # 09:00 Paris = 07:00 UTC in summer
    b = s.book(CHECKUP, "2027-03-26", "15:00", "Bo Li", "0611111111", now=late)
    assert b.start.hour == 14  # 15:00 Paris = 14:00 UTC in winter


def test_two_callers_at_the_same_moment():
    s = fresh()
    results = []

    def attempt(n: int) -> None:
        try:
            s.book("cleaning", "2026-11-17", "09:00", f"Caller {n}", f"06000000{n:02d}", now=NOW)
            results.append("booked")
        except SlotTakenError:
            results.append("refused")

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("booked") == 1, results


def test_delete_patient_data():
    s = fresh()
    s.book(CHECKUP, "2026-11-17", "09:00", "Ana Roy", "0612345678", now=NOW)
    assert s.delete_patient_data("06 12 34 56 78") == 1
    assert s.find("0612345678", now=NOW) == []


def test_tool_text_never_raises():
    tools = BookingTools(fresh())
    tomorrow = (clinic.now().date() + timedelta(days=1)).isoformat()
    assert "Free start times" in tools.check_availability("cleaning", tomorrow) or "No free times" in tools.check_availability("cleaning", tomorrow)
    assert tools.check_availability("braces", tomorrow).startswith("FAILED")
    assert tools.book_appointment("cleaning", "not-a-date", "09:00", "Ana Roy", "0612345678").startswith("FAILED")
    assert "No upcoming appointments" in tools.find_appointments("0612345678")
    assert tools.cancel_appointment("ZZZZZZ", "0612345678").startswith("FAILED")


def test_emergency_appointment_short_notice():
    s = fresh()
    # 10:00 now: an ordinary check-up at 10:30 is too soon, an emergency slot is not
    refuses(lambda: s.book(CHECKUP, "2026-11-16", "10:30", "Ana Roy", "0612345678", now=NOW), "too soon")
    assert s.book("emergency", "2026-11-16", "10:30", "Ana Roy", "0612345678", now=NOW)
    refuses(lambda: s.book("emergency", "2026-11-16", "10:00", "Bo Li", "0611111111", now=NOW), "too soon")


def fresh_callbacks() -> CallbackService:
    return CallbackService(clinic, Path(tempfile.mkdtemp()) / "test.db")


def test_callback_requests():
    c = fresh_callbacks()
    first = c.create("Ana Roy", "06 12 34 56 78", "Wants to ask about braces.")
    assert first.patient_phone == "0612345678" and not first.urgent
    # The same caller again within half an hour updates the first request
    again = c.create("Ana Roy", "0612345678", "Tooth pain since last night.", urgent=True)
    assert again.id == first.id and again.urgent
    c.create("Bo Li", "0611111111", "Question about an invoice.")
    waiting = c.open_requests()
    assert [r.patient_name for r in waiting] == ["Ana Roy", "Bo Li"]  # urgent first
    assert waiting[0].reason == "Tooth pain since last night."
    assert c.mark_done(first.id) and not c.mark_done(first.id)
    assert [r.patient_name for r in c.open_requests()] == ["Bo Li"]
    assert c.delete_patient("0611111111") == 1 and c.open_requests() == []


def test_callback_bad_input_and_tool_text():
    c = fresh_callbacks()
    refuses(lambda: c.create("", "0612345678", "Needs a call."), "name")
    refuses(lambda: c.create("Ana Roy", "0612", "Needs a call."), "phone number")
    refuses(lambda: c.create("Ana Roy", "0612345678", ""), "what the caller needs")
    assert len(c.create("Ana Roy", "0612345678", "x" * 2000).reason) == 500
    assert request_callback_text(c, "Bo Li", "0611111111", "Upset about a bill.", False).startswith("SAVED: Callback request")
    assert request_callback_text(c, "Cy Mo", "0622222222", "Swollen gum.", True).startswith("SAVED: URGENT")
    assert request_callback_text(c, "Cy Mo", "06", "Swollen gum.", True).startswith("FAILED")


def test_late_cancellation_is_flagged():
    from clinic_config import ClinicConfig
    s = fresh()
    original = ClinicConfig.now
    ClinicConfig.now = lambda self: NOW  # Monday 10:00
    try:
        s.book("cleaning", "2026-11-16", "16:00", "Ana Roy", "0612345678", now=NOW)  # today
        s.book(CHECKUP, "2026-11-19", "10:00", "Ana Roy", "0612345678", now=NOW)  # Thursday
        tools = BookingTools(s)
        found = tools.find_appointments("0612345678")
        today_part, later_part = found.split(";")
        assert "late" in today_part and "late" not in later_part, found
        reference = s.find("0612345678", now=NOW)[0].id
        assert "late cancellation" in tools.cancel_appointment(reference, "0612345678")
    finally:
        ClinicConfig.now = original


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok   {test.__name__}")
    print(f"\nAll {len(tests)} tests passed.")