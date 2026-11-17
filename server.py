"""Web server for the clinic widget and the staff admin page.

    uv run uvicorn server:app --port 8000

For visitors:
  - serves the widget script and a demo page,
  - hands the widget a short-lived ticket to join one call (the LiveKit keys
    never leave this server),
  - tells LiveKit which clinic the call is for, so the agent loads the right config.

For clinic staff, at /admin (password in ADMIN_PASSWORD):
  - callback requests, appointments and call transcripts,
  - editing the clinic's details, hours, services and FAQs.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from livekit import api
from pydantic import BaseModel, ValidationError

from booking_service import BookingError, BookingService, SQLiteBackend, normalise_phone
from call_log import CallLog
from callback_service import CallbackService
from clinic_config import ClinicConfig, load_clinic, save_clinic

load_dotenv(".env.local")

AGENT_NAME = "clinic-receptionist"
BOOKINGS_DB = os.getenv("BOOKINGS_DB", "data/bookings.db")
WEB_DIR = Path(__file__).parent / "web"
CALL_TICKET_MINUTES = 10  # how long a ticket can be used to start a call
MAX_CALLS = 8  # per visitor ...
PER_SECONDS = 600  # ... in this many seconds

app = FastAPI(title="Clinic voice widget", docs_url=None, redoc_url=None)
_recent_calls: dict[str, deque[float]] = defaultdict(deque)


class CallRequest(BaseModel):
    clinic_id: str


def _clinic_or_404(clinic_id: str) -> ClinicConfig:
    try:
        return load_clinic(clinic_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="Unknown clinic.") from None


def _check_origin(clinic: ClinicConfig, request: Request) -> str:
    """Returns the origin to allow, or refuses a website the clinic has not listed."""
    origin = request.headers.get("origin", "")
    if not clinic.allowed_origins:
        return origin or "*"
    own_origin = str(request.base_url).rstrip("/")
    if origin and origin not in clinic.allowed_origins and origin != own_origin:
        raise HTTPException(status_code=403, detail="This website may not use the widget.")
    return origin or own_origin


def _check_rate(request: Request) -> None:
    visitor = request.client.host if request.client else "unknown"
    now = time.monotonic()
    calls = _recent_calls[visitor]
    while calls and now - calls[0] > PER_SECONDS:
        calls.popleft()
    if len(calls) >= MAX_CALLS:
        raise HTTPException(
            status_code=429, detail="Too many calls. Please try again in a few minutes."
        )
    calls.append(now)


def _cors(origin: str) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Vary": "Origin",
    }


@app.options("/api/{path:path}")
def preflight(request: Request) -> Response:
    return Response(status_code=204, headers=_cors(request.headers.get("origin", "*")))


@app.get("/api/clinics/{clinic_id}")
def clinic_public_info(clinic_id: str, request: Request) -> JSONResponse:
    """The little the widget needs to show before a call starts."""
    clinic = _clinic_or_404(clinic_id)
    origin = _check_origin(clinic, request)
    return JSONResponse({"id": clinic.id, "name": clinic.name}, headers=_cors(origin))


@app.post("/api/call")
def start_call(body: CallRequest, request: Request) -> JSONResponse:
    clinic = _clinic_or_404(body.clinic_id)
    origin = _check_origin(clinic, request)
    _check_rate(request)

    livekit_url = os.getenv("LIVEKIT_URL")
    if not (livekit_url and os.getenv("LIVEKIT_API_KEY") and os.getenv("LIVEKIT_API_SECRET")):
        raise HTTPException(status_code=500, detail="The call service is not configured.")

    room = f"{clinic.id}-{secrets.token_hex(6)}"
    token = (
        api.AccessToken()  # reads LIVEKIT_API_KEY and LIVEKIT_API_SECRET
        .with_identity(f"visitor-{secrets.token_hex(4)}")
        .with_ttl(timedelta(minutes=CALL_TICKET_MINUTES))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_room_config(
            api.RoomConfiguration(
                # Close the room quickly once the visitor leaves
                empty_timeout=30,
                departure_timeout=10,
                max_participants=2,
                agents=[
                    api.RoomAgentDispatch(
                        agent_name=AGENT_NAME,
                        metadata=json.dumps({"clinic_id": clinic.id}),
                    )
                ],
            )
        )
        .to_jwt()
    )
    return JSONResponse({"url": livekit_url, "token": token}, headers=_cors(origin))


@app.get("/widget.js")
def widget_script() -> FileResponse:
    return FileResponse(
        WEB_DIR / "widget.js",
        media_type="application/javascript",
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
    )


@app.get("/")
def demo_page() -> FileResponse:
    return FileResponse(WEB_DIR / "demo.html", media_type="text/html")


# --------------------------------------------------------------------------
# Admin: for clinic staff
# --------------------------------------------------------------------------

_basic = HTTPBasic(realm="Clinic admin")

# The parts of the config staff may change from the admin page. Everything else
# (id, timezone, voice, booking rules, emergency text) stays as set up.
EDITABLE = [
    "name",
    "greeting",
    "address",
    "directions",
    "phone",
    "email",
    "hours",
    "closed_dates",
    "services",
    "policies",
    "faqs",
]


def require_admin(credentials: HTTPBasicCredentials = Depends(_basic)) -> None:
    password = os.getenv("ADMIN_PASSWORD", "")
    if not password:
        raise HTTPException(
            status_code=503, detail="Admin is switched off: set ADMIN_PASSWORD in .env.local."
        )
    name_ok = secrets.compare_digest(credentials.username.encode(), b"admin")
    password_ok = secrets.compare_digest(credentials.password.encode(), password.encode())
    if not (name_ok and password_ok):
        raise HTTPException(
            status_code=401,
            detail="Wrong name or password.",
            headers={"WWW-Authenticate": 'Basic realm="Clinic admin"'},
        )


def _when(clinic: ClinicConfig, moment) -> dict[str, str]:
    local = moment.astimezone(clinic.tz)
    return {"iso": local.isoformat(), "text": f"{local:%a} {local.day} {local:%b}, {local:%H:%M}"}


def _readable_errors(error: ValidationError) -> list[str]:
    messages = []
    for item in error.errors():
        where = " > ".join(str(part) for part in item["loc"])
        text = item["msg"].removeprefix("Value error, ")
        messages.append(f"{where}: {text}" if where else text)
    return messages


@app.get("/admin")
def admin_page(_: None = Depends(require_admin)) -> FileResponse:
    return FileResponse(WEB_DIR / "admin.html", media_type="text/html")


@app.get("/admin/api/{clinic_id}/overview")
def admin_overview(clinic_id: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    clinic = _clinic_or_404(clinic_id)
    bookings = BookingService(clinic, SQLiteBackend(BOOKINGS_DB))
    callbacks = CallbackService(clinic, BOOKINGS_DB)
    now = clinic.now()
    upcoming = bookings.backend.booked_between(clinic.id, now, now + timedelta(days=120))
    return {
        "clinic": {"id": clinic.id, "name": clinic.name},
        "callbacks": [
            {
                "id": request.id,
                "when": _when(clinic, request.created),
                "name": request.patient_name,
                "phone": request.patient_phone,
                "reason": request.reason,
                "urgent": request.urgent,
            }
            for request in callbacks.open_requests()
        ],
        "appointments": [
            {
                "id": appointment.id,
                "when": _when(clinic, appointment.start),
                "service": appointment.service,
                "practitioner": appointment.practitioner,
                "name": appointment.patient_name,
                "phone": appointment.patient_phone,
            }
            for appointment in upcoming
        ],
    }


@app.post("/admin/api/{clinic_id}/callbacks/{request_id}/done")
def admin_callback_done(
    clinic_id: str, request_id: str, _: None = Depends(require_admin)
) -> dict[str, bool]:
    clinic = _clinic_or_404(clinic_id)
    if not CallbackService(clinic, BOOKINGS_DB).mark_done(request_id):
        raise HTTPException(status_code=404, detail="That request is no longer open.")
    return {"ok": True}


@app.post("/admin/api/{clinic_id}/appointments/{appointment_id}/cancel")
def admin_cancel_appointment(
    clinic_id: str, appointment_id: str, _: None = Depends(require_admin)
) -> dict[str, bool]:
    clinic = _clinic_or_404(clinic_id)
    backend = SQLiteBackend(BOOKINGS_DB)
    appointment = backend.get(appointment_id.strip().upper())
    if appointment is None or appointment.clinic_id != clinic.id or appointment.status != "booked":
        raise HTTPException(status_code=404, detail="That appointment is no longer booked.")
    backend.cancel(appointment.id)
    return {"ok": True}


@app.get("/admin/api/{clinic_id}/calls")
def admin_calls(clinic_id: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    clinic = _clinic_or_404(clinic_id)
    log = CallLog(clinic, BOOKINGS_DB)
    log.purge_old()
    return {
        "retention_days": clinic.transcript_retention_days,
        "calls": [
            {
                "id": record.id,
                "when": _when(clinic, record.started),
                "minutes": max(1, round((record.ended - record.started).total_seconds() / 60)),
                "preview": next(
                    (line["text"] for line in record.lines if line["who"] == "caller"), ""
                )[:90],
                "lines": record.lines,
            }
            for record in log.recent()
        ],
    }


@app.delete("/admin/api/{clinic_id}/calls/{call_id}")
def admin_delete_call(
    clinic_id: str, call_id: str, _: None = Depends(require_admin)
) -> dict[str, bool]:
    clinic = _clinic_or_404(clinic_id)
    if not CallLog(clinic, BOOKINGS_DB).delete(call_id):
        raise HTTPException(status_code=404, detail="That transcript no longer exists.")
    return {"ok": True}


class ForgetRequest(BaseModel):
    phone: str


@app.post("/admin/api/{clinic_id}/forget")
def admin_forget_patient(
    clinic_id: str, body: ForgetRequest, _: None = Depends(require_admin)
) -> dict[str, int]:
    """Erases everything stored about one phone number: a patient's right to deletion."""
    clinic = _clinic_or_404(clinic_id)
    try:
        phone = normalise_phone(body.phone)
    except BookingError:
        raise HTTPException(status_code=422, detail="That phone number looks incomplete.") from None
    return {
        "appointments": SQLiteBackend(BOOKINGS_DB).delete_patient(clinic.id, phone),
        "callbacks": CallbackService(clinic, BOOKINGS_DB).delete_patient(phone),
        "transcripts": CallLog(clinic, BOOKINGS_DB).delete_mentioning(phone),
    }


@app.get("/admin/api/{clinic_id}/config")
def admin_get_config(clinic_id: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    clinic = _clinic_or_404(clinic_id)
    data = clinic.model_dump(mode="json")
    return {
        "editable": {key: data[key] for key in EDITABLE},
        "practitioners": [person["name"] for person in data["practitioners"]],
    }


@app.put("/admin/api/{clinic_id}/config")
def admin_save_config(
    clinic_id: str, changes: dict[str, Any], _: None = Depends(require_admin)
) -> dict[str, bool]:
    clinic = _clinic_or_404(clinic_id)
    data = clinic.model_dump(mode="json")
    for key in EDITABLE:
        if key in changes:
            data[key] = changes[key]
    try:
        updated = ClinicConfig.model_validate(data)
    except ValidationError as error:
        raise HTTPException(status_code=422, detail=_readable_errors(error)) from None
    save_clinic(updated)  # the agent reads the file at the start of each call
    return {"ok": True}