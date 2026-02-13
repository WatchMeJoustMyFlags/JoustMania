"""Auto-detect ALSA audio card and configure /etc/asound.conf at startup."""

import logging

logger = logging.getLogger(__name__)

ASOUND_CONF_PATH = "/etc/asound.conf"

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


def configure_alsa_device(card: str = "auto") -> None:
    """Detect the ALSA card and write /etc/asound.conf.

    Args:
        card: "auto" to detect via alsaaudio, or a card number string (e.g. "1").
    """
    card_number = _resolve_card_number(card)
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


def _auto_detect_card() -> int:
    """Auto-detect the first available ALSA card, falling back to 0."""
    try:
        import alsaaudio

        cards = alsaaudio.cards()
    except Exception:
        logger.warning("Failed to enumerate ALSA cards, falling back to card 0", exc_info=True)
        return 0

    if not cards:
        logger.warning("No ALSA cards found, falling back to card 0")
        return 0

    logger.info("Detected ALSA cards: %s — using card 0 (%s)", cards, cards[0])
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
