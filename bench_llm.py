"""Measures how long Claude takes to start replying, with and without tools.

Run with:  uv run python bench_llm.py
It sends the same request the agent sends (same prompt, same tools), three ways:
  1. no tools            (how Phase 2 behaved)
  2. tools, strict mode  (how the agent behaves now)
  3. tools, strict off
Costs a few cents. Takes about a minute.
"""

import asyncio
import copy
import statistics
import time

import anthropic as anthropic_sdk
from dotenv import load_dotenv
from livekit.agents import APIConnectOptions
from livekit.agents import llm as lk_llm

load_dotenv(".env.local")

import agent  # noqa: E402  (must come after load_dotenv)
from booking_service import BookingService, SQLiteBackend  # noqa: E402
from booking_tools import BookingTools  # noqa: E402
from callback_service import CallbackService  # noqa: E402
from clinic_config import load_clinic  # noqa: E402

RUNS = 6
QUESTION = "What time do you close today?"


class _Captured(Exception):
    pass


async def capture_request() -> dict:
    """Builds the exact request the agent would send, without sending it."""
    clinic = load_clinic(agent.DEFAULT_CLINIC_ID)
    receptionist = agent.Receptionist(
        clinic,
        BookingTools(BookingService(clinic, SQLiteBackend("data/bench.db"))),
        CallbackService(clinic, "data/bench.db"),
    )
    model = agent.NoThinkingLLM(
        model="claude-haiku-5-5", caching="ephemeral", _strict_tool_schema=False
    )
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        raise _Captured()

    model._client.messages.create = fake_create
    chat_ctx = lk_llm.ChatContext()
    chat_ctx.add_message(role="system", content=receptionist.instructions)
    chat_ctx.add_message(role="user", content=QUESTION)
    stream = model.chat(
        chat_ctx=chat_ctx,
        tools=receptionist.tools,
        conn_options=APIConnectOptions(max_retry=0),
    )
    try:
        async for _ in stream:
            pass
    except Exception:
        pass
    await stream.aclose()
    if "tools" not in captured:
        raise SystemExit("Could not build the request. Paste this output to Claude.")
    return captured


async def time_to_first_token(client, request: dict) -> float:
    started = time.perf_counter()
    stream = await client.messages.create(**request)
    first = None
    async for event in stream:
        if first is None and event.type == "content_block_delta":
            first = time.perf_counter() - started
    return first if first is not None else float("nan")


async def main() -> None:
    base = await capture_request()
    base.pop("timeout", None)

    no_tools = copy.deepcopy(base)
    no_tools.pop("tools", None)
    no_tools.pop("tool_choice", None)

    strict_on = copy.deepcopy(base)
    for tool in strict_on["tools"]:
        tool["strict"] = True
        tool["input_schema"]["additionalProperties"] = False
        tool["input_schema"]["required"] = list(tool["input_schema"]["properties"])
    strict_off = copy.deepcopy(base)
    for tool in strict_off["tools"]:
        tool.pop("strict", None)

    variants = {
        "no tools": no_tools,
        "tools, strict on": strict_on,
        "tools, strict off": strict_off,
    }
    strict_now = any(tool.get("strict") for tool in base["tools"])
    print(f"Agent currently sends strict={strict_now}, {len(base['tools'])} tools.\n")

    client = anthropic_sdk.AsyncClient()
    results: dict[str, list[float]] = {name: [] for name in variants}
    for run in range(RUNS):
        for name, request in variants.items():
            seconds = await time_to_first_token(client, request)
            results[name].append(seconds)
            print(f"run {run + 1}  {name:<18} {seconds:5.2f} s")

    print("\nTime to first token (seconds)")
    print(f"{'variant':<18} {'first run':>10} {'median of rest':>15} {'slowest of rest':>16}")
    for name, values in results.items():
        rest = values[1:]
        print(
            f"{name:<18} {values[0]:>10.2f} {statistics.median(rest):>15.2f} {max(rest):>16.2f}"
        )


if __name__ == "__main__":
    asyncio.run(main())