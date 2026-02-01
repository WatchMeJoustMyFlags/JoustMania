"""
Player Profile Manager for JoustMania (Issue #23)

Manages loading, saving, and updating player profiles in Redis.
"""

import logging
import time

from lib.otel_metrics import Counter, Histogram
from lib.player_profile import PlayerProfile, RoundResult
from lib.redis_client import RedisClient, get_redis_client

logger = logging.getLogger(__name__)

# Metrics (Issue #23: Task #14)
profile_operations_total = Counter(
    "profile_operations_total",
    "Total profile operations",
    ["operation", "status"],  # operation=load/save/update, status=success/failure
)
profile_operation_duration_seconds = Histogram(
    "profile_operation_duration_seconds",
    "Profile operation duration in seconds",
    ["operation"],  # operation=load/save/update
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0],
)
profile_cache_total = Counter(
    "profile_cache_total",
    "Profile cache operations",
    ["result"],  # result=hit/miss/new
)

# Redis key patterns
PROFILE_KEY_PREFIX = "player:{serial}:profile"
HISTORY_KEY_PREFIX = "player:{serial}:history"
SESSION_KEY_PREFIX = "session:{session_id}:players"

# History limits
MAX_HISTORY_ROUNDS = 100


class PlayerProfileManager:
    """
    Manages player profiles in Redis.

    Provides CRUD operations, stats updates, and performance calculations.
    """

    def __init__(self, redis_client: RedisClient | None = None):
        """
        Initialize profile manager.

        Args:
            redis_client: Redis client instance (uses default if None)
        """
        self.redis = redis_client or get_redis_client()

    def _profile_key(self, serial: str) -> str:
        """Get Redis key for player profile."""
        return PROFILE_KEY_PREFIX.replace("{serial}", serial)

    def _history_key(self, serial: str) -> str:
        """Get Redis key for player history."""
        return HISTORY_KEY_PREFIX.replace("{serial}", serial)

    def _session_key(self, session_id: str) -> str:
        """Get Redis key for session players."""
        return SESSION_KEY_PREFIX.replace("{session_id}", session_id)

    def load_profile(self, serial: str) -> PlayerProfile:
        """
        Load player profile from Redis or create new one.

        Args:
            serial: Controller serial number

        Returns:
            PlayerProfile instance (existing or new)
        """
        start_time = time.time()

        try:
            key = self._profile_key(serial)
            data = self.redis.get_json(key)

            if data is None:
                # Create new profile
                profile = PlayerProfile.create_new(serial)
                logger.info(f"Created new profile for {serial}")
                profile_cache_total.labels(result="new").inc()
            else:
                # Load existing profile
                profile = PlayerProfile.from_dict(data)
                logger.debug(f"Loaded profile for {serial} ({profile.total_games} games)")
                profile_cache_total.labels(result="hit").inc()

            load_duration = time.time() - start_time
            logger.debug(f"Profile load took {load_duration*1000:.1f}ms")

            # Record metrics
            profile_operation_duration_seconds.labels(operation="load").observe(load_duration)
            profile_operations_total.labels(operation="load", status="success").inc()

            return profile

        except Exception as e:
            logger.error(f"Failed to load profile for {serial}: {e}")
            # Record failure
            profile_operations_total.labels(operation="load", status="failure").inc()
            # Return new profile as fallback
            return PlayerProfile.create_new(serial)

    def save_profile(self, profile: PlayerProfile) -> bool:
        """
        Save player profile to Redis.

        Args:
            profile: PlayerProfile instance to save

        Returns:
            True if successful, False otherwise
        """
        start_time = time.time()

        try:
            key = self._profile_key(profile.serial)
            data = profile.to_dict()
            success = self.redis.set_json(key, data)

            save_duration = time.time() - start_time
            logger.debug(f"Profile save took {save_duration*1000:.1f}ms")

            # Record metrics
            profile_operation_duration_seconds.labels(operation="save").observe(save_duration)

            if success:
                logger.debug(f"Saved profile for {profile.serial}")
                profile_operations_total.labels(operation="save", status="success").inc()
            else:
                logger.warning(f"Failed to save profile for {profile.serial}")
                profile_operations_total.labels(operation="save", status="failure").inc()

            return success

        except Exception as e:
            logger.error(f"Failed to save profile for {profile.serial}: {e}")
            profile_operations_total.labels(operation="save", status="failure").inc()
            return False

    def update_ffa_stats(
        self,
        serial: str,
        won: bool,
        warnings: int,
        survival_time: float,
    ) -> bool:
        """
        Update FFA stats for a player.

        Args:
            serial: Controller serial number
            won: Did player win?
            warnings: Number of warnings received
            survival_time: Seconds survived

        Returns:
            True if successful, False otherwise
        """
        start_time = time.time()
        try:
            profile = self.load_profile(serial)
            profile.add_ffa_result(won, warnings, survival_time)
            profile.update_performance_metrics()
            success = self.save_profile(profile)

            # Record metrics
            update_duration = time.time() - start_time
            profile_operation_duration_seconds.labels(operation="update_ffa").observe(
                update_duration
            )
            profile_operations_total.labels(
                operation="update_ffa",
                status="success" if success else "failure",
            ).inc()

            return success
        except Exception as e:
            logger.error(f"Failed to update FFA stats for {serial}: {e}")
            profile_operations_total.labels(operation="update_ffa", status="failure").inc()
            return False

    def update_nonstop_stats(
        self,
        serial: str,
        kills: int,
        deaths: int,
        warnings: int,
        best_streak: int,
    ) -> bool:
        """
        Update Nonstop Joust stats for a player.

        Args:
            serial: Controller serial number
            kills: Number of kills
            deaths: Number of deaths
            warnings: Number of warnings
            best_streak: Best kill streak this game

        Returns:
            True if successful, False otherwise
        """
        start_time = time.time()
        try:
            profile = self.load_profile(serial)
            profile.add_nonstop_result(kills, deaths, warnings, best_streak)
            profile.update_performance_metrics()
            success = self.save_profile(profile)

            # Record metrics
            update_duration = time.time() - start_time
            profile_operation_duration_seconds.labels(operation="update_nonstop").observe(
                update_duration
            )
            profile_operations_total.labels(
                operation="update_nonstop",
                status="success" if success else "failure",
            ).inc()

            return success
        except Exception as e:
            logger.error(f"Failed to update Nonstop stats for {serial}: {e}")
            profile_operations_total.labels(operation="update_nonstop", status="failure").inc()
            return False

    def update_team_stats(
        self,
        serial: str,
        won: bool,
        warnings: int,
    ) -> bool:
        """
        Update team mode stats for a player.

        Args:
            serial: Controller serial number
            won: Did player's team win?
            warnings: Number of warnings

        Returns:
            True if successful, False otherwise
        """
        start_time = time.time()
        try:
            profile = self.load_profile(serial)
            profile.add_team_result(won, warnings)
            profile.update_performance_metrics()
            success = self.save_profile(profile)

            # Record metrics
            update_duration = time.time() - start_time
            profile_operation_duration_seconds.labels(operation="update_team").observe(
                update_duration
            )
            profile_operations_total.labels(
                operation="update_team",
                status="success" if success else "failure",
            ).inc()

            return success
        except Exception as e:
            logger.error(f"Failed to update team stats for {serial}: {e}")
            profile_operations_total.labels(operation="update_team", status="failure").inc()
            return False

    def add_round_to_history(
        self,
        serial: str,
        round_result: RoundResult,
    ) -> bool:
        """
        Add a round result to player's history.

        Maintains a maximum of MAX_HISTORY_ROUNDS rounds using FIFO.

        Args:
            serial: Controller serial number
            round_result: Round result to add

        Returns:
            True if successful, False otherwise
        """
        try:
            key = self._history_key(serial)

            # Add to head of list
            self.redis.lpush_json(key, round_result.to_dict())

            # Trim to max size
            self.redis.ltrim(key, 0, MAX_HISTORY_ROUNDS - 1)

            logger.debug(f"Added round to history for {serial}")
            return True

        except Exception as e:
            logger.error(f"Failed to add round to history for {serial}: {e}")
            return False

    def get_round_history(
        self,
        serial: str,
        limit: int = MAX_HISTORY_ROUNDS,
    ) -> list[RoundResult]:
        """
        Get player's round history.

        Args:
            serial: Controller serial number
            limit: Maximum number of rounds to return

        Returns:
            List of RoundResult objects (newest first)
        """
        try:
            key = self._history_key(serial)
            data = self.redis.lrange_json(key, 0, limit - 1)
            return [RoundResult.from_dict(d) for d in data]
        except Exception as e:
            logger.error(f"Failed to get round history for {serial}: {e}")
            return []

    def add_to_session(self, session_id: str, serial: str) -> bool:
        """
        Add player to active session.

        Args:
            session_id: Session identifier
            serial: Controller serial number

        Returns:
            True if successful, False otherwise
        """
        try:
            key = self._session_key(session_id)
            self.redis.sadd(key, serial)
            logger.debug(f"Added {serial} to session {session_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to add {serial} to session: {e}")
            return False

    def remove_from_session(self, session_id: str, serial: str) -> bool:
        """
        Remove player from active session.

        Args:
            session_id: Session identifier
            serial: Controller serial number

        Returns:
            True if successful, False otherwise
        """
        try:
            key = self._session_key(session_id)
            self.redis.srem(key, serial)
            logger.debug(f"Removed {serial} from session {session_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to remove {serial} from session: {e}")
            return False

    def get_session_players(self, session_id: str) -> set[str]:
        """
        Get all players in a session.

        Args:
            session_id: Session identifier

        Returns:
            Set of controller serial numbers
        """
        try:
            key = self._session_key(session_id)
            return self.redis.smembers(key)
        except Exception as e:
            logger.error(f"Failed to get session players: {e}")
            return set()

    def delete_profile(self, serial: str) -> bool:
        """
        Delete player profile and history (for testing/admin).

        Args:
            serial: Controller serial number

        Returns:
            True if successful, False otherwise
        """
        try:
            profile_key = self._profile_key(serial)
            history_key = self._history_key(serial)

            profile_deleted = self.redis.delete(profile_key)
            history_deleted = self.redis.delete(history_key)

            if profile_deleted or history_deleted:
                logger.info(f"Deleted profile and history for {serial}")
                return True
            logger.warning(f"No profile found to delete for {serial}")
            return False

        except Exception as e:
            logger.error(f"Failed to delete profile for {serial}: {e}")
            return False


# Global singleton instance
_profile_manager: PlayerProfileManager | None = None


def get_profile_manager(redis_client: RedisClient | None = None) -> PlayerProfileManager:
    """
    Get or create the global profile manager instance.

    Args:
        redis_client: Redis client instance (uses default if None)

    Returns:
        PlayerProfileManager instance
    """
    global _profile_manager
    if _profile_manager is None:
        _profile_manager = PlayerProfileManager(redis_client)
    return _profile_manager
