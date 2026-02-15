"""Tests for lib/grpc_utils.py"""

import sys
from pathlib import Path
from unittest.mock import patch

# Add lib to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib.grpc_utils import get_optimized_channel_options, get_server_options, is_local_address


class TestIsLocalAddress:
    """Tests for is_local_address function."""

    def test_localhost(self):
        assert is_local_address("localhost:50051") is True

    def test_127_0_0_1(self):
        assert is_local_address("127.0.0.1:50051") is True

    def test_127_x_range(self):
        assert is_local_address("127.0.1.1:50051") is True

    def test_ipv6_loopback_not_supported(self):
        # Current implementation splits on ":" so IPv6 "::1:port" isn't detected
        assert is_local_address("::1:50051") is False

    def test_unix_socket(self):
        assert is_local_address("unix:/tmp/test.sock") is True

    def test_remote_address(self):
        assert is_local_address("game-coordinator:50053") is False

    def test_empty_string(self):
        assert is_local_address("") is False

    def test_remote_ip(self):
        assert is_local_address("192.168.1.100:50051") is False


class TestGetOptimizedChannelOptions:
    """Tests for get_optimized_channel_options function."""

    def test_with_compression(self):
        options = get_optimized_channel_options(enable_compression=True)
        option_names = [name for name, _ in options]

        assert "grpc.default_compression_algorithm" in option_names
        assert "grpc.keepalive_time_ms" in option_names

    def test_without_compression(self):
        options = get_optimized_channel_options(enable_compression=False)
        option_names = [name for name, _ in options]

        assert "grpc.default_compression_algorithm" not in option_names
        # Other options should still be present
        assert "grpc.keepalive_time_ms" in option_names
        assert "grpc.max_receive_message_length" in option_names


class TestGetServerOptions:
    """Tests for get_server_options function."""

    def test_returns_options(self):
        options = get_server_options()

        assert len(options) > 0
        option_names = [name for name, _ in options]
        assert "grpc.keepalive_time_ms" in option_names
        assert "grpc.max_receive_message_length" in option_names

    def test_ping_interval_set(self):
        options = get_server_options()
        option_dict = dict(options)

        assert option_dict["grpc.http2.min_recv_ping_interval_without_data_ms"] == 20000


class TestCreateChannel:
    """Tests for create_channel function."""

    @patch("lib.grpc_utils.grpc.aio.insecure_channel")
    def test_creates_channel_with_defaults(self, mock_channel):
        from lib.grpc_utils import create_channel

        create_channel("localhost:50051", enable_tracing=False, enable_metrics=False)

        mock_channel.assert_called_once()
        args, kwargs = mock_channel.call_args
        assert args[0] == "localhost:50051"

    @patch("lib.grpc_utils.grpc.aio.insecure_channel")
    def test_local_address_disables_compression(self, mock_channel):
        from lib.grpc_utils import create_channel

        create_channel("localhost:50051", enable_tracing=False, enable_metrics=False)

        _, kwargs = mock_channel.call_args
        option_names = [name for name, _ in kwargs["options"]]
        # Local address should not have compression options
        assert "grpc.default_compression_algorithm" not in option_names
