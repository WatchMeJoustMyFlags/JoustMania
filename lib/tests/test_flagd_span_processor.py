"""Tests for FlagdSpanEnrichmentProcessor."""

from unittest.mock import MagicMock, patch

import lib.flagd_span_processor as span_proc_module
from lib.flagd_span_processor import FlagdSpanEnrichmentProcessor

PATCH_TARGET = "lib.feature_flags.get_flag_client"


class TestFlagdSpanEnrichmentProcessor:
    def setup_method(self):
        self.processor = FlagdSpanEnrichmentProcessor()
        # Reset module-level flag_values cache so tests are independent
        span_proc_module._flag_values_cache = {}
        span_proc_module._flag_values_last_fetch = 0.0

    @patch(PATCH_TARGET)
    def test_no_attributes_when_disabled(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_object_value.return_value = {
            "enabled": False,
            "attribute_sets": [],
        }
        mock_get_client.return_value = mock_client

        span = MagicMock()
        self.processor.on_start(span)
        span.set_attribute.assert_not_called()

    @patch(PATCH_TARGET)
    def test_service_identity_attributes(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_object_value.return_value = {
            "enabled": True,
            "attribute_sets": ["service_identity"],
        }
        mock_get_client.return_value = mock_client

        span = MagicMock()
        self.processor.on_start(span)

        attr_calls = {call[0][0]: call[0][1] for call in span.set_attribute.call_args_list}
        assert attr_calls["demo.enriched"] is True
        assert attr_calls["demo.language"] == "python"
        assert "demo.service" in attr_calls
        assert "demo.namespace" in attr_calls

    @patch(PATCH_TARGET)
    def test_full_enrichment_adds_all_sets(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_object_value.return_value = {
            "enabled": True,
            "attribute_sets": [
                "service_identity",
                "flag_values",
                "runtime",
                "system",
            ],
        }
        mock_client.get_float_value.return_value = 1.0
        mock_client.get_boolean_value.return_value = False
        mock_get_client.return_value = mock_client

        span = MagicMock()
        self.processor.on_start(span)

        attr_names = {call[0][0] for call in span.set_attribute.call_args_list}
        assert "demo.enriched" in attr_names
        assert "demo.flag.trace_sampling_rate" in attr_names
        assert "demo.runtime.pid" in attr_names
        assert "demo.system.hostname" in attr_names

    @patch(PATCH_TARGET, side_effect=Exception("flagd down"))
    def test_graceful_when_flagd_unavailable(self, _mock):
        span = MagicMock()
        self.processor.on_start(span)
        span.set_attribute.assert_not_called()

    def test_force_flush_returns_true(self):
        assert self.processor.force_flush() is True
