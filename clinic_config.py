"""Loads and validates a clinic's config file.

One YAML file per clinic in the clinics/ folder. Nothing about a clinic is
hard-coded anywhere else.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

CLINICS_DIR = Path(__file__).parent / "clinics"
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_HOURS_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")
_ID_PATTERN = re.compile(r"^[a-z0-9-]+$")


class ClosedDate(BaseModel):
    date: date
    reason: str


class Practitioner(BaseModel):
    name: str
    role: str


class Service(BaseModel):
    name: str
    duration_minutes: int = Field(gt=0, le=480)
    price_eur: float | None = Field(default=None, ge=0)
    price_note: str | None = None
    description: str | None = None
    # Who can perform this service. Empty means it cannot be booked by the agent.
    practitioners: list[str] = []
    # Overrides the clinic-wide minimum notice, e.g. for emergency appointments.
    min_notice_minutes: int | None = Field(default=None, ge=0)


class BookingRules(BaseModel):
    slot_minutes: int = Field(default=15, gt=0, le=120)
    buffer_minutes: int = Field(default=5, ge=0, le=120)
    min_notice_minutes: int = Field(default=60, ge=0)
    max_days_ahead: int = Field(default=60, gt=0, le=365)
    # Cancelling with less notice than this is "late": the agent is told to
    # mention the clinic's cancellation policy.
    late_cancellation_hours: int = Field(default=24, ge=0, le=168)


class Faq(BaseModel):
    q: str
    a: str


class Emergency(BaseModel):
    life_threatening: str
    out_of_hours: str


class StaffContact(BaseModel):
    email: str


class ClinicConfig(BaseModel):
    id: str
    name: str
    specialty: str
    timezone: str
    languages: list[str]
    voice: str
    greeting: str
    address: str
    directions: str | None = None
    phone: str
    email: str | None = None
    hours: dict[str, str]
    closed_dates: list[ClosedDate] = []
    practitioners: list[Practitioner] = []
    booking: BookingRules = BookingRules()
    services: list[Service]
    policies: list[str] = []
    faqs: list[Faq] = []
    emergency: Emergency
    staff_contact: StaffContact
    # How long call transcripts are kept before automatic deletion. 0 = never stored.
    transcript_retention_days: int = Field(default=30, ge=0, le=365)
    # Websites allowed to embed the widget, e.g. ["https://www.myclinic.fr"].
    # Empty means any website, which is fine for a demo only.
    allowed_origins: list[str] = []

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _ID_PATTERN.match(value):
            raise ValueError("id may only contain lowercase letters, digits and hyphens")
        return value

    @field_validator("greeting")
    @classmethod
    def _check_greeting(cls, value: str) -> str:
        # Callers must be told at the start that they are talking to an AI
        if not re.search(r"\bAI\b|artificial intelligence|automated|virtual assistant", value, re.I):
            raise ValueError("the greeting must say that the caller is talking to an AI assistant")
        return " ".join(value.split())

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # raises if the timezone name is unknown
        return value

    @field_validator("hours")
    @classmethod
    def _check_hours(cls, value: dict[str, str]) -> dict[str, str]:
        missing = [day for day in DAYS if day not in value]
        if missing:
            raise ValueError(f"hours is missing: {', '.join(missing)}")
        for day, hours in value.items():
            if day not in DAYS:
                raise ValueError(f"unknown day in hours: {day}")
            if hours != "closed" and not _HOURS_PATTERN.match(hours):
                raise ValueError(f"{day}: use HH:MM-HH:MM or 'closed', got '{hours}'")
        return value

    @model_validator(mode="after")
    def _check_service_practitioners(self) -> "ClinicConfig":
        known = {p.name for p in self.practitioners}
        for service in self.services:
            unknown = [name for name in service.practitioners if name not in known]
            if unknown:
                raise ValueError(
                    f"service '{service.name}' lists unknown practitioners: {', '.join(unknown)}"
                )
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        """Current date and time at the clinic."""
        return datetime.now(ZoneInfo(self.timezone))


def clinic_path(clinic_id: str) -> Path:
    if not _ID_PATTERN.match(clinic_id):
        raise ValueError(f"invalid clinic id: {clinic_id!r}")
    return CLINICS_DIR / f"{clinic_id}.yaml"


def save_clinic(clinic: ClinicConfig) -> None:
    """Writes the config back to its file, keeping the previous version as .bak."""
    path = clinic_path(clinic.id)
    text = yaml.safe_dump(
        clinic.model_dump(mode="json"), allow_unicode=True, sort_keys=False, width=100
    )
    if path.exists():
        path.with_suffix(".yaml.bak").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)  # swap in one step, so a call never reads a half-written file


def load_clinic(clinic_id: str) -> ClinicConfig:
    path = clinic_path(clinic_id)
    if not path.exists():
        raise FileNotFoundError(f"no config file for clinic '{clinic_id}' at {path}")
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return ClinicConfig.model_validate(data)