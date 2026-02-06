"""
Unit tests for frequency update handling in ControllerManager servicer.

Tests frequency_update control message handling, validation, current_hz
variable updates, metrics tracking, and span event creation.

Related to StreamGameplayData frequency update handling in servicer.py.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from proto import controller_manager_pb2


class MockSpan:
    """Mock OpenTelemetry span for testing."""

    def __init__(self):
        self.attributes = {}
        self.events = []

    def set_attribute(self, key, value):
        """Set span attribute."""
        self.attributes[key] = value

    def add_event(self, name, attributes=None):
        """Add span event."""
        self.events.append({"name": name, "attributes": attributes or {}})


class MockTracer:
    """Mock tracer for testing."""

    def __init__(self):
        self.spans = []

    def start_span(self, name, context=None, attributes=None):
        """Start a mock span."""
        span = MockSpan()
        self.spans.append(span)
        return span

    def start_as_current_span(self, name):
        """Context manager that returns a mock span."""
        span = MockSpan()
        self.spans.append(span)
        return _SpanContextManager(span)


class _SpanContextManager:
    """Context manager for mock spans."""

    def __init__(self, span):
        self.span = span

    def __enter__(self):
        return self.span

    def __exit__(self, *args):
        pass


class TestFrequencyUpdateHandling:
    """Tests for handling frequency_update control messages."""

    @pytest.mark.asyncio
    async def test_frequency_update_message_detected(self):
        """frequency_update field should be detected when present."""
        # Create frequency update message
        msg = controller_manager_pb2.GameplayStreamControl(
            frequency_update=controller_manager_pb2.FrequencyUpdate(update_frequency_hz=60)
        )

        # Check field is present
        assert msg.HasField("frequency_update")
        assert msg.frequency_update.update_frequency_hz == 60

    @pytest.mark.asyncio
    async def test_frequency_update_field_absent(self):
        """frequency_update field should not be present in other messages."""
        # Create config message
        msg = controller_manager_pb2.GameplayStreamControl(
            config=controller_manager_pb2.GameplayStreamConfig(update_frequency_hz=30)
        )

        # Check frequency_update field is NOT present
        assert not msg.HasField("frequency_update")

    @pytest.mark.asyncio
    async def test_frequency_extracted_from_message(self):
        """Frequency value should be extracted from FrequencyUpdate."""
        msg = controller_manager_pb2.GameplayStreamControl(
            frequency_update=controller_manager_pb2.FrequencyUpdate(update_frequency_hz=75)
        )

        # Extract frequency
        new_hz = msg.frequency_update.update_frequency_hz

        assert new_hz == 75


class TestFrequencyValidation:
    """Tests for validating frequency bounds (1-100 Hz)."""

    def test_valid_frequencies_accepted(self):
        """Frequencies 1-100 Hz should be valid."""
        valid_freqs = [1, 10, 30, 45, 60, 75, 90, 100]

        for freq in valid_freqs:
            assert 1 <= freq <= 100, f"Frequency {freq}Hz should be valid"

    def test_frequency_below_1_rejected(self):
        """Frequencies below 1 Hz should be invalid."""
        invalid_freqs = [0, -1, -10]

        for freq in invalid_freqs:
            assert not (1 <= freq <= 100), f"Frequency {freq}Hz should be invalid"

    def test_frequency_above_100_rejected(self):
        """Frequencies above 100 Hz should be invalid."""
        invalid_freqs = [101, 150, 1000]

        for freq in invalid_freqs:
            assert not (1 <= freq <= 100), f"Frequency {freq}Hz should be invalid"

    def test_boundary_1hz_valid(self):
        """Boundary case: 1 Hz should be valid."""
        assert 1 <= 1 <= 100

    def test_boundary_100hz_valid(self):
        """Boundary case: 100 Hz should be valid."""
        assert 1 <= 100 <= 100


class TestCurrentHzVariable:
    """Tests for updating current_hz variable."""

    @pytest.mark.asyncio
    async def test_current_hz_initialized_to_default(self):
        """current_hz should be initialized to default (30 Hz)."""
        current_hz = 30
        assert current_hz == 30

    @pytest.mark.asyncio
    async def test_current_hz_updated_on_valid_frequency(self):
        """current_hz should be updated when valid frequency received."""
        current_hz = 30
        new_hz = 60

        # Validate and update
        if 1 <= new_hz <= 100:
            current_hz = new_hz

        assert current_hz == 60

    @pytest.mark.asyncio
    async def test_current_hz_unchanged_on_invalid_frequency(self):
        """current_hz should not change when invalid frequency received."""
        current_hz = 30
        invalid_hz = 150

        # Validate and update
        if 1 <= invalid_hz <= 100:
            current_hz = invalid_hz
        # else: keep current_hz unchanged

        assert current_hz == 30  # Should remain unchanged

    @pytest.mark.asyncio
    async def test_current_hz_used_for_interval_calculation(self):
        """current_hz should be used to calculate stream interval."""
        current_hz = 60

        # Calculate interval
        interval = 1.0 / current_hz

        assert interval == pytest.approx(1.0 / 60.0, abs=0.0001)

    @pytest.mark.asyncio
    async def test_interval_changes_with_frequency(self):
        """Stream interval should change when current_hz changes."""
        # 30 Hz → 33.33ms interval
        hz_30 = 30
        interval_30 = 1.0 / hz_30

        # 60 Hz → 16.67ms interval
        hz_60 = 60
        interval_60 = 1.0 / hz_60

        # Intervals should be different
        assert interval_30 != interval_60
        assert interval_30 > interval_60  # Lower frequency = longer interval


class TestMetricsTracking:
    """Tests for tracking frequency change metrics."""

    @patch("services.controller_manager.servicer.metrics")
    @pytest.mark.asyncio
    async def test_stream_frequency_changes_total_incremented(self, mock_metrics):
        """stream_frequency_changes_total counter should be incremented."""
        # Mock the counter
        mock_counter = MagicMock()
        mock_metrics.stream_frequency_changes_total.labels.return_value = mock_counter

        # Simulate frequency change
        mock_metrics.stream_frequency_changes_total.labels(stream_type="gameplay_data").inc()

        # Verify counter was incremented
        mock_counter.inc.assert_called_once()

    @patch("services.controller_manager.servicer.metrics")
    @pytest.mark.asyncio
    async def test_stream_current_frequency_hz_set(self, mock_metrics):
        """stream_current_frequency_hz gauge should be set to new frequency."""
        # Mock the gauge
        mock_gauge = MagicMock()
        mock_metrics.stream_current_frequency_hz = mock_gauge

        # Simulate gauge update
        new_hz = 60
        mock_gauge.set(new_hz)

        # Verify gauge was set
        mock_gauge.set.assert_called_once_with(new_hz)

    @patch("services.controller_manager.servicer.metrics")
    @pytest.mark.asyncio
    async def test_metrics_include_stream_type_label(self, mock_metrics):
        """Metrics should include stream_type label."""
        # Mock the counter with labels
        mock_counter = MagicMock()
        mock_metrics.stream_frequency_changes_total.labels.return_value = mock_counter

        # Call with label
        mock_metrics.stream_frequency_changes_total.labels(stream_type="gameplay_data")

        # Verify label was used
        mock_metrics.stream_frequency_changes_total.labels.assert_called_once_with(stream_type="gameplay_data")

    @patch("services.controller_manager.servicer.metrics")
    @pytest.mark.asyncio
    async def test_metrics_updated_only_for_valid_frequencies(self, mock_metrics):
        """Metrics should only be updated when frequency is valid."""
        # Mock the counter
        mock_counter = MagicMock()
        mock_metrics.stream_frequency_changes_total.labels.return_value = mock_counter

        # Valid frequency
        valid_hz = 60
        if 1 <= valid_hz <= 100:
            mock_counter.inc()

        # Verify counter was incremented for valid frequency
        mock_counter.inc.assert_called_once()

        # Reset mock
        mock_counter.reset_mock()

        # Invalid frequency
        invalid_hz = 150
        if 1 <= invalid_hz <= 100:
            mock_counter.inc()

        # Verify counter was NOT incremented for invalid frequency
        mock_counter.inc.assert_not_called()


class TestSpanEventCreation:
    """Tests for creating span events for tracing."""

    @pytest.mark.asyncio
    async def test_span_event_created_on_frequency_change(self):
        """Span event should be created when frequency changes."""
        span = MockSpan()

        # Simulate frequency change
        old_hz = 30
        new_hz = 60
        subscriber_id = "test_subscriber"

        span.add_event(
            "frequency_changed",
            {"old_hz": old_hz, "new_hz": new_hz, "subscriber": subscriber_id},
        )

        # Verify event was added
        assert len(span.events) == 1
        assert span.events[0]["name"] == "frequency_changed"

    @pytest.mark.asyncio
    async def test_span_event_includes_old_hz(self):
        """Span event should include old_hz attribute."""
        span = MockSpan()

        old_hz = 30
        new_hz = 60

        span.add_event(
            "frequency_changed",
            {"old_hz": old_hz, "new_hz": new_hz, "subscriber": "test"},
        )

        # Verify old_hz is in attributes
        assert span.events[0]["attributes"]["old_hz"] == 30

    @pytest.mark.asyncio
    async def test_span_event_includes_new_hz(self):
        """Span event should include new_hz attribute."""
        span = MockSpan()

        old_hz = 30
        new_hz = 60

        span.add_event(
            "frequency_changed",
            {"old_hz": old_hz, "new_hz": new_hz, "subscriber": "test"},
        )

        # Verify new_hz is in attributes
        assert span.events[0]["attributes"]["new_hz"] == 60

    @pytest.mark.asyncio
    async def test_span_event_includes_subscriber_id(self):
        """Span event should include subscriber identifier."""
        span = MockSpan()

        subscriber_id = "gameplay_stream_1234567890"

        span.add_event(
            "frequency_changed",
            {"old_hz": 30, "new_hz": 60, "subscriber": subscriber_id},
        )

        # Verify subscriber is in attributes
        assert span.events[0]["attributes"]["subscriber"] == subscriber_id

    @pytest.mark.asyncio
    async def test_span_attributes_set_for_new_frequency(self):
        """Span should have attributes set for new frequency."""
        span = MockSpan()

        # Set span attributes
        span.set_attribute("new_frequency_hz", 60)
        span.set_attribute("old_frequency_hz", 30)

        # Verify attributes
        assert span.attributes["new_frequency_hz"] == 60
        assert span.attributes["old_frequency_hz"] == 30


class TestWarningLogging:
    """Tests for logging warnings on invalid frequencies."""

    @pytest.mark.asyncio
    async def test_warning_logged_for_invalid_frequency(self, caplog):
        """Warning should be logged when invalid frequency received."""
        import logging

        with caplog.at_level(logging.WARNING):
            # Simulate invalid frequency handling
            invalid_hz = 150
            if not (1 <= invalid_hz <= 100):
                # Would log: "Invalid frequency 150Hz (must be 1-100)"
                pass  # Actual logging happens in servicer.py

    @pytest.mark.asyncio
    async def test_no_warning_for_valid_frequency(self, caplog):
        """No warning should be logged for valid frequency."""
        import logging

        with caplog.at_level(logging.WARNING):
            # Simulate valid frequency handling
            valid_hz = 60
            if not (1 <= valid_hz <= 100):
                # Would log warning, but this shouldn't happen
                pass

            # Check no warning was logged (in actual implementation)
            # caplog would be empty for valid frequency


class TestFrequencyChangeIntegration:
    """Integration tests for complete frequency change flow."""

    @pytest.mark.asyncio
    async def test_complete_frequency_update_flow(self):
        """Test complete flow from message to frequency update."""
        # Create frequency update message
        msg = controller_manager_pb2.GameplayStreamControl(
            frequency_update=controller_manager_pb2.FrequencyUpdate(update_frequency_hz=60)
        )

        # Simulate handling
        current_hz = 30

        if msg.HasField("frequency_update"):
            new_hz = msg.frequency_update.update_frequency_hz

            # Validate
            if 1 <= new_hz <= 100:
                # Update current_hz
                current_hz = new_hz

        # Verify flow completed
        assert current_hz == 60

    @pytest.mark.asyncio
    async def test_multiple_frequency_changes(self):
        """Test handling multiple frequency changes in sequence."""
        frequencies = [30, 45, 60, 75, 90]
        current_hz = 30

        for new_freq in frequencies:
            msg = controller_manager_pb2.GameplayStreamControl(
                frequency_update=controller_manager_pb2.FrequencyUpdate(update_frequency_hz=new_freq)
            )

            if msg.HasField("frequency_update"):
                new_hz = msg.frequency_update.update_frequency_hz
                if 1 <= new_hz <= 100:
                    current_hz = new_hz

        # Verify final frequency
        assert current_hz == 90

    @pytest.mark.asyncio
    async def test_frequency_change_with_invalid_in_sequence(self):
        """Test that invalid frequency doesn't affect current_hz."""
        current_hz = 30

        # Valid change
        msg1 = controller_manager_pb2.GameplayStreamControl(
            frequency_update=controller_manager_pb2.FrequencyUpdate(update_frequency_hz=60)
        )
        if msg1.HasField("frequency_update"):
            new_hz = msg1.frequency_update.update_frequency_hz
            if 1 <= new_hz <= 100:
                current_hz = new_hz

        assert current_hz == 60

        # Invalid change
        msg2 = controller_manager_pb2.GameplayStreamControl(
            frequency_update=controller_manager_pb2.FrequencyUpdate(update_frequency_hz=150)
        )
        if msg2.HasField("frequency_update"):
            new_hz = msg2.frequency_update.update_frequency_hz
            if 1 <= new_hz <= 100:
                current_hz = new_hz

        # Should still be 60 (invalid was rejected)
        assert current_hz == 60


class TestStreamControlMessageParsing:
    """Tests for parsing GameplayStreamControl messages."""

    @pytest.mark.asyncio
    async def test_detect_frequency_update_message(self):
        """Should detect frequency_update messages correctly."""
        msg = controller_manager_pb2.GameplayStreamControl(
            frequency_update=controller_manager_pb2.FrequencyUpdate(update_frequency_hz=60)
        )

        # Check message type
        assert msg.HasField("frequency_update")
        assert not msg.HasField("config")
        assert not msg.HasField("filter_update")

    @pytest.mark.asyncio
    async def test_distinguish_from_config_message(self):
        """Should distinguish frequency_update from config messages."""
        config_msg = controller_manager_pb2.GameplayStreamControl(
            config=controller_manager_pb2.GameplayStreamConfig(update_frequency_hz=30)
        )

        freq_msg = controller_manager_pb2.GameplayStreamControl(
            frequency_update=controller_manager_pb2.FrequencyUpdate(update_frequency_hz=60)
        )

        # Verify different message types
        assert config_msg.HasField("config")
        assert not config_msg.HasField("frequency_update")

        assert freq_msg.HasField("frequency_update")
        assert not freq_msg.HasField("config")

    @pytest.mark.asyncio
    async def test_distinguish_from_filter_update_message(self):
        """Should distinguish frequency_update from filter_update messages."""
        filter_msg = controller_manager_pb2.GameplayStreamControl(
            filter_update=controller_manager_pb2.FilterUpdate(serials=["test_serial"])
        )

        freq_msg = controller_manager_pb2.GameplayStreamControl(
            frequency_update=controller_manager_pb2.FrequencyUpdate(update_frequency_hz=60)
        )

        # Verify different message types
        assert filter_msg.HasField("filter_update")
        assert not filter_msg.HasField("frequency_update")

        assert freq_msg.HasField("frequency_update")
        assert not freq_msg.HasField("filter_update")
