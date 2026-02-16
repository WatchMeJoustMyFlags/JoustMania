"""
Unit tests for normalize_serial() in lib/controller_constants.py.
"""

from lib.controller_constants import normalize_serial


class TestNormalizeSerialMAC:
    """Tests for MAC-format serial normalization."""

    def test_lowercase_with_colons(self):
        assert normalize_serial("e0:ae:5e:e1:11:ab") == "E0AE5EE111AB"

    def test_uppercase_with_colons(self):
        assert normalize_serial("E0:AE:5E:E1:11:AB") == "E0AE5EE111AB"

    def test_mixed_case_with_colons(self):
        assert normalize_serial("e0:AE:5e:E1:11:Ab") == "E0AE5EE111AB"

    def test_uppercase_no_colons(self):
        assert normalize_serial("E0AE5EE111AB") == "E0AE5EE111AB"

    def test_lowercase_no_colons(self):
        assert normalize_serial("e0ae5ee111ab") == "E0AE5EE111AB"

    def test_all_zeros(self):
        assert normalize_serial("00:00:00:00:00:00") == "000000000000"

    def test_all_f(self):
        assert normalize_serial("ff:ff:ff:ff:ff:ff") == "FFFFFFFFFFFF"

    def test_idempotent(self):
        """Normalizing an already normalized serial returns the same value."""
        normalized = normalize_serial("e0:ae:5e:e1:11:ab")
        assert normalize_serial(normalized) == normalized

    def test_idempotent_multiple_times(self):
        result = "E0AE5EE111AB"
        for _ in range(5):
            result = normalize_serial(result)
        assert result == "E0AE5EE111AB"


class TestNormalizeSerialNonMAC:
    """Tests for non-MAC serials that should pass through unchanged."""

    def test_mock_controller_serial(self):
        assert normalize_serial("mock_controller_0") == "mock_controller_0"

    def test_mock_uppercase(self):
        assert normalize_serial("MOCK0000") == "MOCK0000"

    def test_test_serial(self):
        assert normalize_serial("TEST_SERIAL_001") == "TEST_SERIAL_001"

    def test_short_string(self):
        assert normalize_serial("ABC") == "ABC"

    def test_long_string(self):
        assert normalize_serial("ABCDEF123456789") == "ABCDEF123456789"

    def test_empty_string(self):
        assert normalize_serial("") == ""

    def test_twelve_chars_non_hex(self):
        """12-char string with non-hex chars should pass through."""
        assert normalize_serial("HELLO_WORLD!") == "HELLO_WORLD!"

    def test_twelve_chars_with_g(self):
        """12 uppercase chars with 'G' (non-hex) should pass through."""
        assert normalize_serial("AABBCCDDEEFG") == "AABBCCDDEEFG"
