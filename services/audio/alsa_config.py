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
