"""Checks the clean-up applied to the agent's words before speech.

Run with:  uv run python test_speech_text.py
"""

import asyncio

from speech_text import clean_stream, clean_text

# The confirmation from a failed run of eval b08
B08 = (
    "Let me confirm: that's 06, 12, 34, 56, 78.\n\n"
    "Here's what I'll book:\n"
    "1. A check-up and consultation with Dr Claire Moreau, tomorrow, at nine in the morning.\n"
    "2. A scale and polish cleaning with Sophie Bernard, tomorrow, at twelve noon.\n\n"
    "Both under the name Marie Dupont. Shall I go ahead?"
)
B08_SPOKEN = (
    "Let me confirm: that's 06, 12, 34, 56, 78. "
    "Here's what I'll book: "
    "A check-up and consultation with Dr Claire Moreau, tomorrow, at nine in the morning. "
    "A scale and polish cleaning with Sophie Bernard, tomorrow, at twelve noon. "
    "Both under the name Marie Dupont. Shall I go ahead?"
)


def every_split(text: str) -> list[list[str]]:
    """The text cut into chunks in different ways, as the LLM stream might."""
    splits = [[text], list(text)]
    for size in (2, 3, 5, 7, 11):
        splits.append([text[i : i + size] for i in range(0, len(text), size)])
    return splits


def test_numbered_list_becomes_one_flow():
    for chunks in every_split(B08):
        assert clean_text(chunks) == B08_SPOKEN, chunks


def test_bullets_and_brackets_are_dropped():
    assert clean_text(["Options:\n- nine\n- ten"]) == "Options: nine, ten"
    assert clean_text(["* first\n• second"]) == "first, second"
    assert clean_text(["1) a check-up\n2) a cleaning"]) == "a check-up, a cleaning"


def test_ordinary_speech_is_untouched():
    for text in [
        "Your check-up is booked for Friday at eleven in the morning.",
        "10.30 is free, and so is 11.",
        "-5 is not a marker, nor is 2026 at the start.",
        "Is that the right appointment?",
        "A dash - in the middle stays, as does 1. in the middle.",
    ]:
        for chunks in every_split(text):
            assert clean_text(chunks) == text, (text, chunks)


def test_line_starting_with_a_number_that_is_not_a_marker():
    assert clean_text(["Free times:\n9 in the morning\n10 in the morning"]) == (
        "Free times: 9 in the morning, 10 in the morning"
    )


def test_reply_ending_on_held_back_text():
    assert clean_text(["See you\n12"]) == "See you, 12"
    assert clean_text(["Okay.\n- "]) == "Okay."


def test_stream_matches_whole_text():
    async def chunks():
        for ch in B08:
            yield ch

    async def collect():
        return "".join([part async for part in clean_stream(chunks())])

    assert asyncio.run(collect()) == B08_SPOKEN


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok   {test.__name__}")
    print(f"\nAll {len(tests)} tests passed.")
