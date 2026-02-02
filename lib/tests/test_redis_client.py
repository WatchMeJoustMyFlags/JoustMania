"""
Unit tests for redis_client.py

Tests Redis client wrapper with retry logic and JSON serialization.
"""

import json
from unittest.mock import Mock, patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from lib.redis_client import (
    RETRY_ATTEMPTS,
    RedisClient,
    get_redis_client,
)


class TestRedisClientInit:
    """Tests for RedisClient initialization and connection."""

    @patch("lib.redis_client.redis.Redis")
    def test_successful_connection(self, mock_redis_class):
        """Test successful Redis connection on first attempt."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_redis_class.return_value = mock_client

        client = RedisClient(host="localhost", port=6379, db=0)

        assert client._client == mock_client
        mock_redis_class.assert_called_once()
        mock_client.ping.assert_called_once()

    @patch("lib.redis_client.redis.Redis")
    @patch("lib.redis_client.time.sleep")
    def test_connection_retry_success(self, mock_sleep, mock_redis_class):
        """Test connection succeeds after retry."""
        mock_client = Mock()
        mock_client.ping.side_effect = [
            RedisConnectionError("Connection failed"),
            True,  # Success on second attempt
        ]
        mock_redis_class.return_value = mock_client

        client = RedisClient()

        assert client._client == mock_client
        assert mock_client.ping.call_count == 2
        mock_sleep.assert_called_once()

    @patch("lib.redis_client.redis.Redis")
    @patch("lib.redis_client.time.sleep")
    def test_connection_retry_exhausted(self, mock_sleep, mock_redis_class):
        """Test connection fails after all retries exhausted."""
        mock_client = Mock()
        mock_client.ping.side_effect = RedisConnectionError("Connection failed")
        mock_redis_class.return_value = mock_client

        with pytest.raises(RedisConnectionError):
            RedisClient()

        assert mock_client.ping.call_count == RETRY_ATTEMPTS


class TestRedisClientPing:
    """Tests for ping() method."""

    @patch("lib.redis_client.redis.Redis")
    def test_ping_success(self, mock_redis_class):
        """Test successful ping."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        assert client.ping() is True

    @patch("lib.redis_client.redis.Redis")
    def test_ping_failure(self, mock_redis_class):
        """Test ping returns False on Redis error."""
        mock_client = Mock()
        mock_client.ping.side_effect = [True, RedisError("Ping failed")]
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        assert client.ping() is False

    @patch("lib.redis_client.redis.Redis")
    @patch("lib.redis_client.time.sleep")
    def test_ping_triggers_reconnect(self, mock_sleep, mock_redis_class):
        """Test ping triggers reconnect when connection is lost."""
        mock_client = Mock()
        # First ping succeeds (init), second fails (lost), third succeeds (reconnected)
        mock_client.ping.side_effect = [
            True,  # Init
            RedisConnectionError("Connection lost"),  # Ping check
            True,  # Reconnect
            True,  # Actual ping
        ]
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.ping()

        assert result is True
        # ping called 4 times: init + check + reconnect + actual
        assert mock_client.ping.call_count == 4


class TestRedisClientJsonOperations:
    """Tests for JSON serialization/deserialization operations."""

    @patch("lib.redis_client.redis.Redis")
    def test_set_json_success(self, mock_redis_class):
        """Test setting JSON value."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.set.return_value = True
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.set_json("test_key", {"foo": "bar"})

        assert result is True
        mock_client.set.assert_called_once_with(
            "test_key", '{"foo": "bar"}', ex=None
        )

    @patch("lib.redis_client.redis.Redis")
    def test_set_json_with_expiration(self, mock_redis_class):
        """Test setting JSON value with expiration."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.set.return_value = True
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.set_json("test_key", {"foo": "bar"}, ex=3600)

        assert result is True
        mock_client.set.assert_called_once_with(
            "test_key", '{"foo": "bar"}', ex=3600
        )

    @patch("lib.redis_client.redis.Redis")
    def test_set_json_redis_error(self, mock_redis_class):
        """Test set_json returns False on Redis error."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.set.side_effect = RedisError("Set failed")
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.set_json("test_key", {"foo": "bar"})

        assert result is False

    @patch("lib.redis_client.redis.Redis")
    def test_set_json_serialization_error(self, mock_redis_class):
        """Test set_json returns False on JSON serialization error."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        # Non-serializable object
        result = client.set_json("test_key", Mock())

        assert result is False

    @patch("lib.redis_client.redis.Redis")
    def test_get_json_success(self, mock_redis_class):
        """Test getting JSON value."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.get.return_value = '{"foo": "bar"}'
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.get_json("test_key")

        assert result == {"foo": "bar"}
        mock_client.get.assert_called_once_with("test_key")

    @patch("lib.redis_client.redis.Redis")
    def test_get_json_not_found(self, mock_redis_class):
        """Test get_json returns default when key doesn't exist."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.get.return_value = None
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.get_json("missing_key", default={"default": "value"})

        assert result == {"default": "value"}

    @patch("lib.redis_client.redis.Redis")
    def test_get_json_redis_error(self, mock_redis_class):
        """Test get_json returns default on Redis error."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.get.side_effect = RedisError("Get failed")
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.get_json("test_key", default=None)

        assert result is None

    @patch("lib.redis_client.redis.Redis")
    def test_get_json_deserialization_error(self, mock_redis_class):
        """Test get_json returns default on JSON deserialization error."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.get.return_value = "invalid json"
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.get_json("test_key", default={"error": "fallback"})

        assert result == {"error": "fallback"}


class TestRedisClientKeyOperations:
    """Tests for key existence and deletion."""

    @patch("lib.redis_client.redis.Redis")
    def test_exists_true(self, mock_redis_class):
        """Test exists returns True when key exists."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.exists.return_value = 1
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        assert client.exists("test_key") is True

    @patch("lib.redis_client.redis.Redis")
    def test_exists_false(self, mock_redis_class):
        """Test exists returns False when key doesn't exist."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.exists.return_value = 0
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        assert client.exists("missing_key") is False

    @patch("lib.redis_client.redis.Redis")
    def test_exists_error(self, mock_redis_class):
        """Test exists returns False on Redis error."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.exists.side_effect = RedisError("Exists failed")
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        assert client.exists("test_key") is False

    @patch("lib.redis_client.redis.Redis")
    def test_delete_success(self, mock_redis_class):
        """Test successful key deletion."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.delete.return_value = 1
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        assert client.delete("test_key") is True

    @patch("lib.redis_client.redis.Redis")
    def test_delete_error(self, mock_redis_class):
        """Test delete returns False on Redis error."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.delete.side_effect = RedisError("Delete failed")
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        assert client.delete("test_key") is False


class TestRedisClientListOperations:
    """Tests for list operations (lpush, lrange, ltrim)."""

    @patch("lib.redis_client.redis.Redis")
    def test_lpush_json_success(self, mock_redis_class):
        """Test pushing JSON values to list."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.lpush.return_value = 2
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.lpush_json("test_list", {"event": "game_start"}, {"event": "game_end"})

        assert result == 2
        mock_client.lpush.assert_called_once()
        # Check serialized values
        call_args = mock_client.lpush.call_args[0]
        assert call_args[0] == "test_list"
        assert json.loads(call_args[1]) == {"event": "game_start"}
        assert json.loads(call_args[2]) == {"event": "game_end"}

    @patch("lib.redis_client.redis.Redis")
    def test_lpush_json_error(self, mock_redis_class):
        """Test lpush_json returns 0 on Redis error."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.lpush.side_effect = RedisError("Push failed")
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.lpush_json("test_list", {"event": "test"})

        assert result == 0

    @patch("lib.redis_client.redis.Redis")
    def test_lrange_json_success(self, mock_redis_class):
        """Test getting range of JSON values from list."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.lrange.return_value = ['{"event": "game_start"}', '{"event": "game_end"}']
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.lrange_json("test_list", 0, -1)

        assert result == [{"event": "game_start"}, {"event": "game_end"}]
        mock_client.lrange.assert_called_once_with("test_list", 0, -1)

    @patch("lib.redis_client.redis.Redis")
    def test_lrange_json_error(self, mock_redis_class):
        """Test lrange_json returns empty list on Redis error."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.lrange.side_effect = RedisError("Range failed")
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.lrange_json("test_list", 0, -1)

        assert result == []

    @patch("lib.redis_client.redis.Redis")
    def test_ltrim_success(self, mock_redis_class):
        """Test successful list trim."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.ltrim.return_value = True
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.ltrim("test_list", 0, 99)

        assert result is True
        mock_client.ltrim.assert_called_once_with("test_list", 0, 99)

    @patch("lib.redis_client.redis.Redis")
    def test_ltrim_error(self, mock_redis_class):
        """Test ltrim returns False on Redis error."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.ltrim.side_effect = RedisError("Trim failed")
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.ltrim("test_list", 0, 99)

        assert result is False


class TestRedisClientSetOperations:
    """Tests for set operations (sadd, smembers, srem)."""

    @patch("lib.redis_client.redis.Redis")
    def test_sadd_success(self, mock_redis_class):
        """Test adding members to set."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.sadd.return_value = 2
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.sadd("test_set", "member1", "member2")

        assert result == 2
        mock_client.sadd.assert_called_once_with("test_set", "member1", "member2")

    @patch("lib.redis_client.redis.Redis")
    def test_sadd_error(self, mock_redis_class):
        """Test sadd returns 0 on Redis error."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.sadd.side_effect = RedisError("Add failed")
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.sadd("test_set", "member1")

        assert result == 0

    @patch("lib.redis_client.redis.Redis")
    def test_smembers_success(self, mock_redis_class):
        """Test getting all set members."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.smembers.return_value = {"member1", "member2", "member3"}
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.smembers("test_set")

        assert result == {"member1", "member2", "member3"}
        mock_client.smembers.assert_called_once_with("test_set")

    @patch("lib.redis_client.redis.Redis")
    def test_smembers_error(self, mock_redis_class):
        """Test smembers returns empty set on Redis error."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.smembers.side_effect = RedisError("Members failed")
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.smembers("test_set")

        assert result == set()

    @patch("lib.redis_client.redis.Redis")
    def test_srem_success(self, mock_redis_class):
        """Test removing members from set."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.srem.return_value = 2
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.srem("test_set", "member1", "member2")

        assert result == 2
        mock_client.srem.assert_called_once_with("test_set", "member1", "member2")

    @patch("lib.redis_client.redis.Redis")
    def test_srem_error(self, mock_redis_class):
        """Test srem returns 0 on Redis error."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.srem.side_effect = RedisError("Remove failed")
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        result = client.srem("test_set", "member1")

        assert result == 0


class TestRedisClientClose:
    """Tests for connection close."""

    @patch("lib.redis_client.redis.Redis")
    def test_close_success(self, mock_redis_class):
        """Test successful connection close."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        client.close()

        mock_client.close.assert_called_once()

    @patch("lib.redis_client.redis.Redis")
    def test_close_error(self, mock_redis_class):
        """Test close handles Redis error gracefully."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_client.close.side_effect = RedisError("Close failed")
        mock_redis_class.return_value = mock_client

        client = RedisClient()
        # Should not raise exception
        client.close()


class TestGetRedisClient:
    """Tests for get_redis_client() singleton."""

    def setup_method(self):
        """Reset singleton before each test."""
        import lib.redis_client
        lib.redis_client._redis_client = None

    @patch("lib.redis_client.redis.Redis")
    def test_singleton_creation(self, mock_redis_class):
        """Test singleton is created on first call."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_redis_class.return_value = mock_client

        client1 = get_redis_client()
        client2 = get_redis_client()

        assert client1 is client2
        # Redis class called only once
        assert mock_redis_class.call_count == 1

    @patch("lib.redis_client.redis.Redis")
    def test_singleton_with_custom_params(self, mock_redis_class):
        """Test singleton created with custom parameters."""
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_redis_class.return_value = mock_client

        client = get_redis_client(host="custom_host", port=1234, db=5)

        # Note: subsequent calls will return the same instance,
        # ignoring new parameters (singleton behavior)
        assert client._client == mock_client
        mock_redis_class.assert_called_once_with(
            host="custom_host",
            port=1234,
            db=5,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
