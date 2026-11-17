import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    MetricsCollectedEvent,
    RunContext,
    TurnHandlingOptions,
    function_tool,
    metrics,
)
from livekit.agents.beta import EndCallTool
from livekit.plugins import anthropic, deepgram, silero

from booking_service import BookingService, SQLiteBackend
from booking_tools import BookingTools
from call_log import CallLog, lines_from_history
from callback_service import CallbackService, request_callback_text
from clinic_config import ClinicConfig, load_clinic
from prompts import build_instructions
from speech_text import clean_stream

load_dotenv(".env.local")
logger = logging.getLogger("clinic-agent")

# One config file per clinic in clinics/. The widget says which clinic a call is
# for; this default is used in the terminal, where there is no widget.
DEFAULT_CLINIC_ID = os.getenv("CLINIC_ID", "riviere-dental")
# Where appointments and callback requests are stored for the demo
BOOKINGS_DB = os.getenv("BOOKINGS_DB", "data/bookings.db")


class NoThinkingLLM(anthropic.LLM):
    """Claude with thinking switched off.

    Temporary: the LiveKit Anthropic plugin (1.8.12) has no thinking option,
    but it forwards extra_kwargs to the API call. Remove once the plugin
    supports it directly.
    """

    def chat(self, **kwargs):
        given = kwargs.get("extra_kwargs")
        extra = dict(given) if isinstance(given, dict) else {}
        extra.setdefault("thinking", {"type": "disabled"})
        kwargs["extra_kwargs"] = extra
        return super().chat(**kwargs)


class Receptionist(Agent):
    """The tools below are the only way Claude can see or change appointments.

    Each one hands over to BookingTools, which checks every input against the
    clinic's rules and always returns text, including when something fails.
    """

    def __init__(
        self, clinic: ClinicConfig, booking: BookingTools, callbacks: CallbackService
    ) -> None:
        super().__init__(
            instructions=build_instructions(clinic),
            # Lets the agent hang up: it says goodbye, then the call closes
            tools=[EndCallTool(end_instructions="Say one short goodbye.")],
        )
        self.clinic = clinic
        self.booking = booking
        self.callbacks = callbacks

    async def tts_node(self, text, model_settings):
        # The prompt bans line breaks and numbered lists, but if Claude slips,
        # join the lines and drop the "1." markers so they are never spoken.
        async for frame in Agent.default.tts_node(self, clean_stream(text), model_settings):
            yield frame

    @function_tool()
    async def check_availability(
        self,
        context: RunContext,
        service: str,
        date: str,
        practitioner: str | None = None,
    ) -> str:
        """List the free start times for a service on one day.

        Args:
            service: The service name, as written in the clinic's services list.
            date: The day to check, as YYYY-MM-DD.
            practitioner: Leave empty unless the caller named the person they want to see. Never fill this in yourself.
        """
        return await asyncio.to_thread(
            self.booking.check_availability, service, date, practitioner
        )

    @function_tool()
    async def book_appointment(
        self,
        context: RunContext,
        service: str,
        date: str,
        time: str,
        patient_name: str,
        patient_phone: str,
        practitioner: str | None = None,
    ) -> str:
        """Book an appointment. Call this only after the caller has clearly said yes
        to the service, day, time and name you read back to them.

        Args:
            service: The service name, as written in the clinic's services list.
            date: The day, as YYYY-MM-DD.
            time: The start time, as 24-hour HH:MM. Must be a time that check_availability returned.
            patient_name: The caller's full name.
            patient_phone: The caller's phone number, digits only.
            practitioner: Leave empty unless the caller named the person they want to see. Never fill this in yourself.
        """
        return await asyncio.to_thread(
            self.booking.book_appointment,
            service,
            date,
            time,
            patient_name,
            patient_phone,
            practitioner,
        )

    @function_tool()
    async def find_appointments(self, context: RunContext, patient_phone: str) -> str:
        """Look up the upcoming appointments booked with a phone number.
        Use this before rescheduling or cancelling.

        Args:
            patient_phone: The phone number the appointment was booked with, digits only.
        """
        return await asyncio.to_thread(self.booking.find_appointments, patient_phone)

    @function_tool()
    async def reschedule_appointment(
        self,
        context: RunContext,
        appointment_reference: str,
        patient_phone: str,
        new_date: str,
        new_time: str,
    ) -> str:
        """Move an existing appointment to a new day and time. Call this only after
        the caller has clearly said yes to the change you read back to them.

        Args:
            appointment_reference: The reference returned by find_appointments.
            patient_phone: The phone number the appointment was booked with, digits only.
            new_date: The new day, as YYYY-MM-DD.
            new_time: The new start time, as 24-hour HH:MM. Must be a time that check_availability returned.
        """
        return await asyncio.to_thread(
            self.booking.reschedule_appointment,
            appointment_reference,
            patient_phone,
            new_date,
            new_time,
        )

    @function_tool()
    async def cancel_appointment(
        self, context: RunContext, appointment_reference: str, patient_phone: str
    ) -> str:
        """Cancel an existing appointment. Call this only after the caller has
        clearly said yes to cancelling the appointment you described to them.

        Args:
            appointment_reference: The reference returned by find_appointments.
            patient_phone: The phone number the appointment was booked with, digits only.
        """
        return await asyncio.to_thread(
            self.booking.cancel_appointment, appointment_reference, patient_phone
        )

    @function_tool()
    async def request_callback(
        self,
        context: RunContext,
        patient_name: str,
        patient_phone: str,
        reason: str,
        urgent: bool,
    ) -> str:
        """Leave a request for clinic staff to call the caller back. Call this only
        after the caller has agreed to what you will pass on.

        Args:
            patient_name: The caller's name.
            patient_phone: The number staff should call, digits only.
            reason: One or two plain sentences on what the caller needs.
            urgent: True only for pain, swelling, bleeding, injury or a problem after treatment.
        """
        return await asyncio.to_thread(
            request_callback_text, self.callbacks, patient_name, patient_phone, reason, urgent
        )


def clinic_id_for(ctx: agents.JobContext) -> str:
    """The clinic this call is for. server.py puts it in the call's metadata."""
    try:
        wanted = json.loads(ctx.job.metadata or "{}").get("clinic_id")
    except (ValueError, AttributeError):
        wanted = None
    return wanted if isinstance(wanted, str) and wanted else DEFAULT_CLINIC_ID


server = AgentServer()


@server.rtc_session(agent_name="clinic-receptionist")
async def entrypoint(ctx: agents.JobContext):
    # Loaded per call, so edits to the config apply from the next call on
    clinic = load_clinic(clinic_id_for(ctx))
    booking = BookingTools(BookingService(clinic, SQLiteBackend(BOOKINGS_DB)))
    callbacks = CallbackService(clinic, BOOKINGS_DB)
    call_log = CallLog(clinic, BOOKINGS_DB)
    started = datetime.now(timezone.utc)
    logger.info("starting call for clinic %s", clinic.id)

    session = AgentSession(
        turn_handling=TurnHandlingOptions(
            # Flux decides when the patient has finished speaking
            turn_detection="stt",
            # Flux already decides end of turn; don't add a second wait on top
            endpointing={"min_delay": 0},
        ),
        stt=deepgram.STTv2(
            model="flux-general-en",
            # Lets Claude start drafting before the turn is fully confirmed
            eager_eot_threshold=0.5,
        ),
        llm=NoThinkingLLM(
            model="claude-haiku-5-5",
            caching="ephemeral",
            # Strict tool mode added about 1.2 s to every reply (measured with
            # bench_llm.py). It is not needed: booking_service.py validates
            # every tool input itself.
            _strict_tool_schema=False,
        ),
        tts=deepgram.TTS(model=clinic.voice),
        # VAD detects the patient talking over the agent (barge-in)
        vad=silero.VAD.load(),
    )

    @session.on("metrics_collected")
    def _on_metrics(ev: MetricsCollectedEvent):
        metrics.log_metrics(ev.metrics)

    async def save_transcript() -> None:
        # Runs when the call ends, however it ends
        try:
            lines = lines_from_history(session.history.items)
            await asyncio.to_thread(call_log.save, started, lines)
        except Exception:
            logger.exception("could not save the call transcript")

    ctx.add_shutdown_callback(save_transcript)

    await session.start(
        room=ctx.room,
        agent=Receptionist(clinic, booking, callbacks),
        # Keep call audio and transcripts off LiveKit's servers
        record=False,
    )
    await session.say(clinic.greeting)


if __name__ == "__main__":
    agents.cli.run_app(server)