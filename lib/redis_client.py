"""
Redis Client Wrapper for JoustMania (Issue #23)

Provides persistent storage for player profiles, game history, and session management.
"""

import json
import logging
import time
from typing import Any

import redis
from redis.exceptions import ConnectionError, RedisError, TimeoutError

logger = logging.getLogger(__name__)

# Redis connection settings
DEFAULT_HOST = "redis"
DEFAULT_PORT = 6379
DEFAULT_DB = 0
RETRY_ATTEMPTS = 3
RETRY_DELAY = 1.0  # seconds


class RedisClient:
    """
    Wrapper around redis-py with retry logic and JSON serialization.

    Provides high-level operations for player profiles, game history,
    and session management with automatic reconnection on failure.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        db: int = DEFAULT_DB,
        decode_responses: bool = True,
    ):
        """
        Initialize Redis client.

        Args:
            host: Redis server hostname
            port: Redis server port
            db: Redis database number (0-15)
            decode_responses: Decode byte responses to strings
        """
        self.host = host
        self.port = port
        self.db = db
        self.decode_responses = decode_responses
        self._client: redis.Redis | None = None
        self._connect()

    def _connect(self) -> None:
        """Establish connection to Redis with retry logic."""
        for attempt in range(RETRY_ATTEMPTS):
            try:
                self._client = redis.Redis(
                    host=self.host,
                    port=self.port,
                    db=self.db,
                    decode_responses=self.decode_responses,
                    socket_connect_timeout=5,
                    socket_timeout=5,
                )
                # Test connection
                self._client.ping()
                logger.info(f"Connected to Redis at {self.host}:{self.port} (db={self.db})")
                return
            except (ConnectionError, TimeoutError) as e:
                if attempt < RETRY_ATTEMPTS - 1:
                    logger.warning(
                        f"Redis connection attempt {attempt + 1}/{RETRY_ATTEMPTS} failed: {e}. "
                        f"Retrying in {RETRY_DELAY}s..."
                    )
                    time.sleep(RETRY_DELAY)
                else:
                    logger.error(f"Failed to connect to Redis after {RETRY_ATTEMPTS} attempts")
                    raise

    def _reconnect_if_needed(self) -> None:
        """Reconnect if connection is lost."""
        try:
            if self._client:
                self._client.ping()
        except (ConnectionError, TimeoutError):
            logger.warning("Redis connection lost, attempting to reconnect...")
            self._connect()

    def ping(self) -> bool:
        """
        Test Redis connection.

        Returns:
            True if connection is healthy, False otherwise
        """
        try:
            self._reconnect_if_needed()
            return self._client.ping() if self._client else False
        except RedisError:
            return False

    # Key-Value operations with JSON serialization

    def set_json(self, key: str, value: Any, ex: int | None = None) -> bool:
        """
        Set a key to a JSON-serialized value.

        Args:
            key: Redis key
            value: Python object (will be JSON-serialized)
            ex: Expiration time in seconds (optional)

        Returns:
            True if successful, False otherwise
        """
        try:
            self._reconnect_if_needed()
            serialized = json.dumps(value)
            return bool(self._client.set(key, serialized, ex=ex))
        except (RedisError, json.JSONEncodeError) as e:
            logger.error(f"Failed to set JSON key {key}: {e}")
            return False

    def get_json(self, key: str, default: Any = None) -> Any:
        """
        Get a JSON-deserialized value.

        Args:
            key: Redis key
            default: Default value if key doesn't exist

        Returns:
            Deserialized Python object or default
        """
        try:
            self._reconnect_if_needed()
            value = self._client.get(key)
            if value is None:
                return default
            return json.loads(value)
        except (RedisError, json.JSONDecodeError) as e:
            logger.error(f"Failed to get JSON key {key}: {e}")
            return default

    def exists(self, key: str) -> bool:
        """
        Check if a key exists.

        Args:
            key: Redis key

        Returns:
            True if key exists, False otherwise
        """
        try:
            self._reconnect_if_needed()
            return bool(self._client.exists(key))
        except RedisError as e:
            logger.error(f"Failed to check existence of key {key}: {e}")
            return False

    def delete(self, key: str) -> bool:
        """
        Delete a key.

        Args:
            key: Redis key

        Returns:
            True if key was deleted, False otherwise
        """
        try:
            self._reconnect_if_needed()
            return bool(self._client.delete(key))
        except RedisError as e:
            logger.error(f"Failed to delete key {key}: {e}")
            return False

    # List operations (for player history)

    def lpush_json(self, key: str, *values: Any) -> int:
        """
        Push JSON-serialized values to the head of a list.

        Args:
            key: Redis key
            values: Python objects to push

        Returns:
            Length of list after push, or 0 on error
        """
        try:
            self._reconnect_if_needed()
            serialized = [json.dumps(v) for v in values]
            return int(self._client.lpush(key, *serialized))
        except (RedisError, json.JSONEncodeError) as e:
            logger.error(f"Failed to lpush to {key}: {e}")
            return 0

    def lrange_json(self, key: str, start: int, end: int) -> list[Any]:
        """
        Get a range of JSON-deserialized values from a list.

        Args:
            key: Redis key
            start: Start index
            end: End index (-1 for end of list)

        Returns:
            List of deserialized Python objects
        """
        try:
            self._reconnect_if_needed()
            values = self._client.lrange(key, start, end)
            return [json.loads(v) for v in values]
        except (RedisError, json.JSONDecodeError) as e:
            logger.error(f"Failed to lrange from {key}: {e}")
            return []

    def ltrim(self, key: str, start: int, end: int) -> bool:
        """
        Trim a list to the specified range.

        Args:
            key: Redis key
            start: Start index
            end: End index

        Returns:
            True if successful, False otherwise
        """
        try:
            self._reconnect_if_needed()
            return bool(self._client.ltrim(key, start, end))
        except RedisError as e:
            logger.error(f"Failed to ltrim {key}: {e}")
            return False

    # Set operations (for session management)

    def sadd(self, key: str, *members: str) -> int:
        """
        Add members to a set.

        Args:
            key: Redis key
            members: Members to add

        Returns:
            Number of members added
        """
        try:
            self._reconnect_if_needed()
            return int(self._client.sadd(key, *members))
        except RedisError as e:
            logger.error(f"Failed to sadd to {key}: {e}")
            return 0

    def smembers(self, key: str) -> set[str]:
        """
        Get all members of a set.

        Args:
            key: Redis key

        Returns:
            Set of members
        """
        try:
            self._reconnect_if_needed()
            return set(self._client.smembers(key))
        except RedisError as e:
            logger.error(f"Failed to smembers from {key}: {e}")
            return set()

    def srem(self, key: str, *members: str) -> int:
        """
        Remove members from a set.

        Args:
            key: Redis key
            members: Members to remove

        Returns:
            Number of members removed
        """
        try:
            self._reconnect_if_needed()
            return int(self._client.srem(key, *members))
        except RedisError as e:
            logger.error(f"Failed to srem from {key}: {e}")
            return 0

    def close(self) -> None:
        """Close the Redis connection."""
        if self._client:
            try:
                self._client.close()
                logger.info("Redis connection closed")
            except RedisError as e:
                logger.error(f"Error closing Redis connection: {e}")


# Global singleton instance
_redis_client: RedisClient | None = None


def get_redis_client(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    db: int = DEFAULT_DB,
) -> RedisClient:
    """
    Get or create the global Redis client instance.

    Args:
        host: Redis server hostname
        port: Redis server port
        db: Redis database number

    Returns:
        RedisClient instance
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = RedisClient(host=host, port=port, db=db)
    return _redis_client
