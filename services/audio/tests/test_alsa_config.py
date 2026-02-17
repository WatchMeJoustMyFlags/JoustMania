"""Tests for ALSA card auto-detection and configuration."""

from unittest.mock import mock_open, patch

from services.audio.alsa_config import (
    ASOUND_CONF_PATH,
    _auto_detect_card,
    _resolve_card_number,
    _write_asound_conf,
    configure_alsa_device,
)


class TestResolveCardNumber:
    def test_explicit_card_number(self):
        assert _resolve_card_number("1") == 1

    def test_explicit_card_zero(self):
        assert _resolve_card_number("0") == 0

    @patch("services.audio.alsa_config._auto_detect_card", return_value=2)
    def test_auto_calls_detect(self, mock_detect):
        assert _resolve_card_number("auto") == 2
        mock_detect.assert_called_once()

    @patch("services.audio.alsa_config._auto_detect_card", return_value=0)
    def test_invalid_string_falls_back_to_auto(self, mock_detect):
        assert _resolve_card_number("bad") == 0
        mock_detect.assert_called_once()


class TestAutoDetectCard:
    @patch("alsaaudio.card_name", return_value=("Headphones", "Headphones Audio"))
    @patch("alsaaudio.card_indexes", return_value=[0, 1])
    def test_returns_first_card_index(self, mock_indexes, mock_name):
        assert _auto_detect_card() == 0

    @patch("alsaaudio.card_name", return_value=("USB Audio", "USB Audio Device"))
    @patch("alsaaudio.card_indexes", return_value=[2, 5])
    def test_returns_nonzero_first_index(self, mock_indexes, mock_name):
        assert _auto_detect_card() == 2

    @patch("alsaaudio.card_indexes", return_value=[])
    def test_empty_cards_returns_zero(self, mock_indexes):
        assert _auto_detect_card() == 0

    @patch("alsaaudio.card_indexes", side_effect=Exception("no ALSA"))
    def test_exception_returns_zero(self, mock_indexes):
        assert _auto_detect_card() == 0


class TestWriteAsoundConf:
    @patch("builtins.open", mock_open())
    def test_writes_config_with_card_number(self):
        _write_asound_conf(2)
        handle = open(ASOUND_CONF_PATH, "w")  # noqa: SIM115
        written = handle.write.call_args[0][0]
        assert 'pcm "hw:2,0"' in written
        assert "card 2" in written

    @patch("builtins.open", side_effect=OSError("permission denied"))
    def test_handles_write_error(self, mock_file):
        # Should not raise
        _write_asound_conf(0)


class TestConfigureAlsaDevice:
    @patch("services.audio.alsa_config._write_asound_conf")
    @patch("services.audio.alsa_config._resolve_card_number", return_value=1)
    def test_calls_resolve_and_write(self, mock_resolve, mock_write):
        configure_alsa_device("auto")
        mock_resolve.assert_called_once_with("auto")
        mock_write.assert_called_once_with(1)

    @patch("services.audio.alsa_config._write_asound_conf")
    @patch("services.audio.alsa_config._resolve_card_number", return_value=3)
    def test_explicit_card_passed_through(self, mock_resolve, mock_write):
        configure_alsa_device("3")
        mock_resolve.assert_called_once_with("3")
        mock_write.assert_called_once_with(3)
