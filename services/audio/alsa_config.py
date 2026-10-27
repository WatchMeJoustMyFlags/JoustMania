"""Auto-detect ALSA audio card and configure /etc/asound.conf at startup.

Includes retry logic for startup races (when /dev/snd is not yet ready)
and stores the detected card number for fallback PCM device names.
"""

import logging
import time

logger = logging.getLogger(__name__)

ASOUND_CONF_PATH = "/etc/asound.conf"

# Module-level state: the card number detected at startup.
# Used by music_player.py to build fallback device names when dmix fails.
_detected_card: int = 0

ASOUND_TEMPLATE = """\
# ALSA configuration for JoustMania container
# Auto-generated at startup by alsa_config.py — card {card_number}

# Define the dmix plugin for software mixing
pcm.dmixer {{
    type dmix
    ipc_key 1024
    ipc_perm 0666
    slave {{
        pcm "hw:{card_number},0"
        period_time 0
        period_size 1024
        buffer_size 4096
        rate 44100
        channels 2
    }}
}}

# Default output goes through dmix
pcm.!default {{
    type plug
    slave.pcm "dmixer"
}}

ctl.!default {{
    type hw
    card {card_number}
}}
"""

# Retry parameters for auto-detection at startup
_DETECT_MAX_RETRIES = 3
_DETECT_RETRY_DELAY_S = 1.0


def get_detected_card() -> int:
    """Return the card number detected (or configured) at startup."""
    return _detected_card


def configure_alsa_device(card: str = "auto") -> None:
    """Detect the ALSA card and write /etc/asound.conf.

    Args:
        card: "auto" to detect via alsaaudio, or a card number string (e.g. "1").
    """
    global _detected_card
    card_number = _resolve_card_number(card)
    _detected_card = card_number
    _write_asound_conf(card_number)


def _resolve_card_number(card: str) -> int:
    """Resolve the card setting to a numeric card index."""
    if card != "auto":
        try:
            num = int(card)
            logger.info("Using explicit ALSA card number: %d", num)
            return num
        except ValueError:
            logger.warning("Invalid ALSA_CARD value '%s', falling back to auto-detection", card)

    return _auto_detect_card()


def _pick_best_card(indexes: list[int]) -> tuple[int, str]:
    """Pick the best card from available indexes, preferring USB over built-in.

    On Raspberry Pi, HDMI cards (vc4-hdmi) are enumerated first but are
    usually not connected to speakers. USB audio cards are the typical
    output device for JoustMania. This selects the first USB card if one
    exists, otherwise falls back to the first available card.

    Returns:
        (card_index, card_name) tuple.
    """
    import alsaaudio

    cards = []
    for idx in indexes:
        try:
            name = alsaaudio.card_name(idx)[0]
        except Exception:
            name = "unknown"
        cards.append((idx, name))

    for idx, name in cards:
        if "USB" in name.upper():
            logger.info("Detected ALSA cards: %s — preferring USB card %d (%s)", cards, idx, name)
            return idx, name

    idx, name = cards[0]
    logger.info("Detected ALSA cards: %s — no USB card found, using card %d (%s)", cards, idx, name)
    return idx, name


def _auto_detect_card() -> int:
    """Auto-detect the best available ALSA card with retry, falling back to 0.

    Prefers USB audio cards over built-in HDMI. Retries up to
    _DETECT_MAX_RETRIES times with a delay between attempts to handle the
    startup race where /dev/snd is not yet populated (common with USB audio
    on Raspberry Pi).
    """
    for attempt in range(_DETECT_MAX_RETRIES):
        try:
            import alsaaudio

            indexes = alsaaudio.card_indexes()
        except Exception:
            if attempt == _DETECT_MAX_RETRIES - 1:
                logger.warning(
                    "Failed to enumerate ALSA cards after %d attempts, falling back to card 0",
                    _DETECT_MAX_RETRIES,
                    exc_info=True,
                )
                return 0
            logger.info("ALSA card enumeration failed (attempt %d/%d), retrying...", attempt + 1, _DETECT_MAX_RETRIES)
            time.sleep(_DETECT_RETRY_DELAY_S)
            continue

        if not indexes:
            if attempt == _DETECT_MAX_RETRIES - 1:
                logger.warning("No ALSA cards found after %d attempts, falling back to card 0", _DETECT_MAX_RETRIES)
                return 0
            logger.info("No ALSA cards found (attempt %d/%d), retrying...", attempt + 1, _DETECT_MAX_RETRIES)
            time.sleep(_DETECT_RETRY_DELAY_S)
            continue

        card_index, _ = _pick_best_card(indexes)
        return card_index

    return 0


def _write_asound_conf(card_number: int) -> None:
    """Write the asound.conf file with the given card number."""
    content = ASOUND_TEMPLATE.format(card_number=card_number)
    try:
        with open(ASOUND_CONF_PATH, "w") as f:
            f.write(content)
        logger.info("Wrote %s with card %d", ASOUND_CONF_PATH, card_number)
    except OSError:
        logger.warning("Could not write %s — using existing config", ASOUND_CONF_PATH, exc_info=True)


# --- Card-change watcher (UC-031 A6, BR-008, #2198) ---------------------------------------

PROC_CARDS_PATH = "/proc/asound/cards"
CARD_CHANGE_EXIT_CODE = 75  # EX_TEMPFAIL: the compose restart policy restarts the container
CARD_WATCH_INTERVAL_S = 5.0
CARD_STABLE_POLLS = 2  # a change must persist this many polls in a row

_playback_failed: bool = False


def note_playback_failure() -> None:
    """Record that opening or writing the output device failed (read by the card watcher)."""
    global _playback_failed
    _playback_failed = True


def playback_failed() -> bool:
    return _playback_failed


def read_proc_cards(path: str = PROC_CARDS_PATH) -> list[tuple[int, str]]:
    """List (index, description) of the present cards from /proc/asound/cards.

    The description is the driver line, e.g. "USB-Audio - USB Audio", so "USB" matches
    the same way as in _pick_best_card. An unreadable file gives an empty list.
    """
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    cards = []
    for line in lines:
        parts = line.split(None, 1)
        # Card lines look like " 2 [Audio   ]: USB-Audio - USB Audio"; the second line per card is indented text.
        if len(parts) == 2 and parts[0].isdigit() and "[" in parts[1]:
            cards.append((int(parts[0]), parts[1]))
    return cards


def preferred_card(cards: list[tuple[int, str]]) -> int | None:
    """The card the auto rule picks: first USB card, else the first card."""
    for idx, name in cards:
        if "USB" in name.upper():
            return idx
    return cards[0][0] if cards else None


def should_restart_for_card_change(configured: int, cards: list[tuple[int, str]], failed: bool) -> bool:
    """True when the audio service should exit so the restart policy re-detects the card.

    - the preferred card equals the configured one, or no card is present: no.
    - the configured card is still present but another one is preferred (a USB card appeared): yes.
    - the configured card is gone (removed mid-game): only once playback has failed.
    """
    preferred = preferred_card(cards)
    if preferred is None or preferred == configured:
        return False
    if any(idx == configured for idx, _ in cards):
        return True
    return failed


async def watch_card_changes(
    configured: int,
    on_change,
    interval_s: float = CARD_WATCH_INTERVAL_S,
    is_busy=lambda: False,
    max_polls: int | None = None,
) -> None:
    """Poll /proc/asound/cards; log one WARN and call on_change() when a restart is due.

    Keeps only counters and flags between polls (nothing grows per poll). A change must be seen on
    CARD_STABLE_POLLS polls in a row (a flapping card resets the count), and while is_busy() (music
    a game runs, not the lobby) the restart is deferred, with one log line, unless playback has failed.
    max_polls bounds the loop (tests); None = until cancelled.
    """
    import asyncio

    seen = 0
    deferred_logged = False
    polls = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        await asyncio.sleep(interval_s)
        cards = read_proc_cards()
        failed = _playback_failed
        if not should_restart_for_card_change(configured, cards, failed):
            seen = 0
            deferred_logged = False
            continue
        seen += 1
        if not failed and seen < CARD_STABLE_POLLS:
            continue
        if not failed and is_busy():
            if not deferred_logged:
                logger.info("ALSA card change seen (cards %s); restart deferred until the music stops", cards)
                deferred_logged = True
            continue
        logger.warning(
            "ALSA card change: configured card %d, preferred card %s, cards %s, playback_failed=%s; "
            "exiting with code %d so the container restarts and re-detects the card",
            configured,
            preferred_card(cards),
            cards,
            failed,
            CARD_CHANGE_EXIT_CODE,
        )
        on_change()
        return
