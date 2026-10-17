"""
Test helpers for one-shot interventions read as per-game event lists (FR-137,
UC-053 A4 / BR-006).

The flag service renders each one-shot flag (eliminate_player, revive_player,
audio_cue, controller_effect, end_game), for one game's ``gameId``, as
``{"epoch","seq","events":[{"seq","payload"}, ...]}``. Fake flag clients return
these dicts from ``get_object_details``; tests build them from the payload
string the handlers receive (``_event_payload_string``).
"""

from openfeature.flag_evaluation import FlagEvaluationDetails

EDGE_FLAGS = ("eliminate_player", "revive_player", "audio_cue", "controller_effect", "end_game")

# What the flag service serves for a game with no events (the baseline value).
BASELINE = {"epoch": "", "seq": 0, "events": []}


def event_payload(flag_key: str, payload: str) -> dict:
    """Inverse of ``_event_payload_string``: the event payload dict for the
    handler payload string (serial, sound id, ``"<serial>:<effect>"`` or ``""``)."""
    if flag_key == "audio_cue":
        return {"sound": payload} if payload else {}
    if flag_key == "controller_effect":
        serial, _, effect = payload.partition(":")
        return {k: v for k, v in (("serial", serial), ("effect", effect)) if v}
    if flag_key == "end_game":
        return {}
    return {"serial": payload} if payload else {}


# K, the number of events the flag service keeps per list (state.DefaultEventListLen).
EVENT_LIST_LEN = 16


def edge_event(flag_key: str, payload: str = "", seq: int = 1, epoch: str = "e1") -> dict:
    """The event list the flag service serves once ``seq`` events were appended:
    the last K events, ``max(1, seq-K+1)..seq``, all with ``payload``. Earlier
    events stay listed exactly as in production, so a reader that dropped its
    ``seq > last_seq`` filter would apply them again and the tests would see it."""
    first = max(1, seq - EVENT_LIST_LEN + 1)
    events = [{"seq": n, "payload": event_payload(flag_key, payload)} for n in range(first, seq + 1)]
    return {"epoch": epoch, "seq": seq, "events": events}


def edge_events(flag_key: str, *payloads: str, epoch: str = "e1") -> dict:
    """An event list with one event per payload string, seq 1..n."""
    events = [{"seq": i, "payload": event_payload(flag_key, p)} for i, p in enumerate(payloads, start=1)]
    return {"epoch": epoch, "seq": len(events), "events": events}


def object_details(key: str, value: object) -> FlagEvaluationDetails:
    """Details for an event-list read: ``value`` when it is an event list,
    else the baseline (no events)."""
    return FlagEvaluationDetails(flag_key=key, value=value if isinstance(value, dict) else BASELINE)
