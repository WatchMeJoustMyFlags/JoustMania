"""Tests for FlagdSampler."""

from unittest.mock import MagicMock, patch

from opentelemetry.sdk.trace.sampling import Decision

from lib.flagd_sampler import FlagdSampler

PATCH_TARGET = "lib.feature_flags.get_flag_client"


class TestFlagdSampler:
    def setup_method(self):
        self.sampler = FlagdSampler()

    @patch(PATCH_TARGET)
    def test_samples_all_when_rate_is_1(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_float_value.return_value = 1.0
        mock_get_client.return_value = mock_client

        result = self.sampler.should_sample(None, 12345, "test-span")
        assert result.decision == Decision.RECORD_AND_SAMPLE

    @patch(PATCH_TARGET)
    def test_drops_all_when_rate_is_0(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_float_value.return_value = 0.0
        mock_get_client.return_value = mock_client

        result = self.sampler.should_sample(None, 12345, "test-span")
        assert result.decision == Decision.DROP

    @patch(PATCH_TARGET)
    def test_probabilistic_sampling(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_float_value.return_value = 0.5
        mock_get_client.return_value = mock_client

        decisions = [self.sampler.should_sample(None, i, "test-span").decision for i in range(1000)]
        sampled = sum(1 for d in decisions if d == Decision.RECORD_AND_SAMPLE)
        assert 300 < sampled < 700

    @patch(PATCH_TARGET, side_effect=Exception("flagd unavailable"))
    def test_defaults_to_sample_all_when_flagd_unavailable(self, _mock):
        result = self.sampler.should_sample(None, 12345, "test-span")
        assert result.decision == Decision.RECORD_AND_SAMPLE

    @patch(PATCH_TARGET)
    def test_passes_controller_serial_as_context(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_float_value.return_value = 1.0
        mock_get_client.return_value = mock_client

        self.sampler.should_sample(None, 12345, "test-span", attributes={"controller.serial": "AA:BB:CC"})

        call_args = mock_client.get_float_value.call_args
        eval_ctx = call_args[0][2] if len(call_args[0]) > 2 else None
        assert eval_ctx is not None
        assert eval_ctx.attributes["controller_serial"] == "AA:BB:CC"

    def test_get_description(self):
        assert self.sampler.get_description() == "FlagdSampler"
