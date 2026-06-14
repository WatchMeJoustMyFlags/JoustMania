"""
Unit tests for MockControllerService RPC handlers.
"""

import math
import random
import sys
from pathlib import Path

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from lib.controller_constants import AxisKey, ButtonKey, StateKey
from proto import controller_manager_mock_pb2
from services.controller_manager.mock_control_service import MockControllerService
from services.controller_manager.multiplexer.mock_adapter import MockAdapter


def _make_service(num_controllers: int = 0) -> tuple[MockControllerService, MockAdapter]:
    """Create a MockControllerService backed by a MockAdapter."""
    adapter = MockAdapter(num_controllers=num_controllers)
    adapter.discover()
    return MockControllerService(adapter), adapter


class TestAddController:
    def test_add_controller_auto_serial(self):
        service, adapter = _make_service()
        request = controller_manager_mock_pb2.AddControllerRequest()
        response = service.AddController(request, None)
        assert response.success
        assert response.serial == "MOCK0000"
        assert "MOCK0000" in adapter.controllers

    def test_add_controller_with_serial(self):
        service, adapter = _make_service()
        request = controller_manager_mock_pb2.AddControllerRequest(serial="MY_CTRL")
        response = service.AddController(request, None)
        assert response.success
        assert response.serial == "MY_CTRL"
        assert "MY_CTRL" in adapter.controllers

    def test_add_controller_duplicate_returns_existing(self):
        service, _adapter = _make_service()
        request = controller_manager_mock_pb2.AddControllerRequest(serial="DUP")
        service.AddController(request, None)
        response = service.AddController(request, None)
        assert response.success
        assert response.serial == "DUP"


class TestRemoveController:
    def test_remove_existing_controller(self):
        service, adapter = _make_service()
        adapter.add_controller("TO_REMOVE")
        request = controller_manager_mock_pb2.RemoveControllerRequest(serial="TO_REMOVE")
        response = service.RemoveController(request, None)
        assert response.success
        assert "TO_REMOVE" not in adapter.controllers

    def test_remove_nonexistent_controller(self):
        service, _adapter = _make_service()
        request = controller_manager_mock_pb2.RemoveControllerRequest(serial="NOPE")
        response = service.RemoveController(request, None)
        assert not response.success
        assert "not found" in response.error


class TestAddControllers:
    def test_add_multiple_controllers(self):
        service, adapter = _make_service()
        request = controller_manager_mock_pb2.AddControllersRequest(count=3)
        response = service.AddControllers(request, None)
        assert response.success
        assert len(response.serials) == 3
        assert len(adapter.controllers) == 3

    def test_add_zero_controllers(self):
        service, adapter = _make_service()
        request = controller_manager_mock_pb2.AddControllersRequest(count=0)
        response = service.AddControllers(request, None)
        assert response.success
        assert len(response.serials) == 0
        assert len(adapter.controllers) == 0

    def test_add_controllers_incremental(self):
        service, adapter = _make_service()
        # Add 2, then 3 more
        service.AddControllers(controller_manager_mock_pb2.AddControllersRequest(count=2), None)
        response = service.AddControllers(controller_manager_mock_pb2.AddControllersRequest(count=3), None)
        assert response.success
        assert len(response.serials) == 3
        assert len(adapter.controllers) == 5


# Every proto ButtonRequest.Button enum value paired with the backend
# ButtonKey it must drive. The mock SimulateButton API must cover all nine
# physical buttons (#821) — the admin entry combo is the four face buttons held
# together, and admin exit is the PS button.
_BUTTON_CASES = [
    (controller_manager_mock_pb2.ButtonRequest.TRIGGER, ButtonKey.TRIGGER),
    (controller_manager_mock_pb2.ButtonRequest.MOVE, ButtonKey.MOVE),
    (controller_manager_mock_pb2.ButtonRequest.SELECT, ButtonKey.SELECT),
    (controller_manager_mock_pb2.ButtonRequest.START, ButtonKey.START),
    (controller_manager_mock_pb2.ButtonRequest.CROSS, ButtonKey.CROSS),
    (controller_manager_mock_pb2.ButtonRequest.CIRCLE, ButtonKey.CIRCLE),
    (controller_manager_mock_pb2.ButtonRequest.SQUARE, ButtonKey.SQUARE),
    (controller_manager_mock_pb2.ButtonRequest.TRIANGLE, ButtonKey.TRIANGLE),
    (controller_manager_mock_pb2.ButtonRequest.PS, ButtonKey.PS),
]


class TestSimulateButton:
    @pytest.mark.parametrize(("proto_button", "button_key"), _BUTTON_CASES)
    def test_press_sets_backend_state(self, proto_button, button_key):
        """Each of the nine buttons can be pressed and released independently."""
        service, adapter = _make_service()
        adapter.add_controller("BTN")

        request = controller_manager_mock_pb2.ButtonRequest(serial="BTN", button=proto_button, pressed=True)
        response = service.SimulateButton(request, None)
        assert response.success
        assert response.error == ""
        assert adapter.controllers["BTN"][button_key] is True

        release = controller_manager_mock_pb2.ButtonRequest(serial="BTN", button=proto_button, pressed=False)
        response = service.SimulateButton(release, None)
        assert response.success
        assert adapter.controllers["BTN"][button_key] is False

    def test_all_nine_buttons_covered(self):
        """The parametrized cases must enumerate every ButtonKey (no gaps)."""
        covered = {button_key for _proto_button, button_key in _BUTTON_CASES}
        assert covered == set(ButtonKey)

    def test_unknown_controller_returns_error(self):
        service, _adapter = _make_service()
        request = controller_manager_mock_pb2.ButtonRequest(
            serial="MISSING",
            button=controller_manager_mock_pb2.ButtonRequest.CROSS,
            pressed=True,
        )
        response = service.SimulateButton(request, None)
        assert not response.success
        assert "not found" in response.error

    def test_hold_all_four_face_buttons_simultaneously(self):
        """Admin entry combo: CROSS+CIRCLE+SQUARE+TRIANGLE held together.

        Each press is an independent dict key, so no releases are required
        between presses — all four must read True at the same time (#821).
        """
        service, adapter = _make_service()
        adapter.add_controller("ADMIN")

        face_buttons = [
            controller_manager_mock_pb2.ButtonRequest.CROSS,
            controller_manager_mock_pb2.ButtonRequest.CIRCLE,
            controller_manager_mock_pb2.ButtonRequest.SQUARE,
            controller_manager_mock_pb2.ButtonRequest.TRIANGLE,
        ]
        for proto_button in face_buttons:
            response = service.SimulateButton(
                controller_manager_mock_pb2.ButtonRequest(serial="ADMIN", button=proto_button, pressed=True),
                None,
            )
            assert response.success

        controller = adapter.controllers["ADMIN"]
        assert controller[ButtonKey.CROSS] is True
        assert controller[ButtonKey.CIRCLE] is True
        assert controller[ButtonKey.SQUARE] is True
        assert controller[ButtonKey.TRIANGLE] is True
        # Holding the combo must not have disturbed unrelated buttons.
        assert controller[ButtonKey.PS] is False

    def test_ps_button_for_admin_exit(self):
        """Admin exit is the PS button — it must be simulatable on its own."""
        service, adapter = _make_service()
        adapter.add_controller("EXIT")

        response = service.SimulateButton(
            controller_manager_mock_pb2.ButtonRequest(
                serial="EXIT",
                button=controller_manager_mock_pb2.ButtonRequest.PS,
                pressed=True,
            ),
            None,
        )
        assert response.success
        assert adapter.controllers["EXIT"][ButtonKey.PS] is True


# A faithful model of the game-coordinator side of death detection, so a pure
# controller-manager unit test can prove the death actually converges (or that
# the #757 grace re-kill does not happen) under a chosen send cadence WITHOUT
# spinning up the game service. Mirrors services/game_coordinator/games/base.py:
#   - EMA: smoothed = (smoothed*W + raw)/(W+1), weight 4, primed on first frame
#   - death when smoothed > threshold
#   - on kill: not-alive + a respawn grace window during which the EMA is reset
#     to 0.0 every frame (#757 invincibility), then re-primes after grace
#
# The death-detection threshold is per-sensitivity AND per-music-tempo (slow vs
# fast), taken verbatim from base.py SLOW_MAX / FAST_MAX. The mock can be driven
# at ANY of these in a real game (the integration flagd config targets Werewolf
# to "fast" = sensitivity 3, and other modes reach sensitivity 4), so the model
# is parameterized by the resolved threshold rather than pinned to one value.
_EMA_WEIGHT = 4.0
_GRACE_SECONDS = 2.0  # Swapper post-swap respawn grace

# Death thresholds from base.py (g-force), index = sensitivity 0..4.
_SLOW_MAX = [1.3, 1.5, 1.8, 2.5, 3.2]
_FAST_MAX = [1.6, 1.8, 2.8, 3.2, 3.5]
# Sensitivity 2 (MEDIUM) slow-music threshold — the historical integration
# default, used by the cadence/#757 regression tests.
_DEATH_THRESHOLD = _SLOW_MAX[2]  # 1.8g


class _GameSideModel:
    """Minimal game-side EMA + grace model fed by streamed accel frames."""

    def __init__(self, death_threshold: float = _DEATH_THRESHOLD):
        self.smoothed = 0.0
        self.alive = True
        self.grace_until = 0.0
        self.kills = 0
        self.rekills = 0
        self.death_threshold = death_threshold

    def feed(self, accel: dict, now: float) -> None:
        """Process one streamed gameplay frame, exactly as base.py does."""
        raw = math.sqrt(accel[AxisKey.X] ** 2 + accel[AxisKey.Y] ** 2 + accel[AxisKey.Z] ** 2)

        if not self.alive:
            return  # base.py: dead player ignored until respawn

        if now < self.grace_until:
            # base.py resets the EMA every grace frame so the death spike is
            # forgotten before grace expires (#757).
            self.smoothed = 0.0
            return

        # EMA update (prime on first real reading)
        if self.smoothed < 1e-9:
            self.smoothed = raw
        else:
            self.smoothed = (self.smoothed * _EMA_WEIGHT + raw) / (_EMA_WEIGHT + 1)

        if self.smoothed > self.death_threshold:
            self._kill(now)

    def _kill(self, now: float) -> None:
        if self.grace_until == 0.0:
            self.kills += 1
        else:
            # A second kill while a grace window has already been opened is the
            # #757 grace-expiry re-kill regression.
            self.rekills += 1
        self.alive = False  # Swapper marks not-alive, then respawns after grace
        self.grace_until = now + _GRACE_SECONDS

    def respawn_if_grace_started(self, now: float) -> None:
        """Swapper respawns the player partway through the grace window."""
        if self.grace_until > 0.0 and not self.alive:
            self.alive = True


class TestSimulateDeathConverges:
    """Production-state #926 / #757 coverage driven through the real RPC path.

    These tests exercise the ACTUAL production latch state — SimulateDeath sets
    a delivered-frame budget plus the generous idle wall-clock fallback — and
    drive the gameplay send loop at a chosen (possibly STRETCHED) cadence,
    decrementing the latch once per streamed frame via consume_death_frame, just
    like servicer._build_gameplay_update. A faithful game-side EMA+grace model
    then decides life/death. This is what the previous fix never tested: it only
    validated the death_hold_until == 0.0 branch that production never creates.
    """

    def _simulate_in_game_death(
        self,
        send_interval: float,
        wall_seconds: float = 6.0,
        poll_hz: float = 100.0,
        first_send_offset: float = 0.0,
        death_threshold: float = _DEATH_THRESHOLD,
        rest_prime_frames: int = 12,
    ) -> _GameSideModel:
        """Fire SimulateDeath, then run the real TWO-loop data plane against a
        controllable virtual clock and feed the streamed frames to a game-side
        EMA+grace model. Returns the model.

        Two independent loops, exactly as in production:
          - the DISCOVERY poll loop (``poll_hz``, ~100Hz) calls ``poll()`` and
            writes the result into a state cache, just like discovery_loop;
          - the SEND loop (``send_interval`` apart — STRETCHED to simulate
            starvation) reads the latest cached frame, streams it to the game,
            and drains the latch once via ``consume_death_frame`` — exactly like
            servicer._build_gameplay_update.

        Before the death is fired, the model is PRIMED with ``rest_prime_frames``
        resting (~1.0g) frames so its EMA sits in the realistic resting regime
        (~1.0g), exactly like a player who has been alive and still for a moment
        before the kill. This is essential: an EMA primed from 0.0 jumps straight
        to the full ~10g death input on its very first frame (modelling "the
        first frame is the death"), which crosses ANY threshold and hides the
        resting-primed ceiling that the #926 re-review exposed.

        This is what reproduces the #926 failure on the OLD code: with a
        wall-clock-only 1.0s hold, the poll loop reverts the cache to noise
        after 1.0s, so a send loop whose interval exceeds 1.0s reads a stale
        NOISE frame and the death is skipped entirely (zero death frames
        streamed). The frame-budget latch keeps poll() reporting death until the
        SEND loop has drained the budget, so the death always lands.
        """
        service, adapter = _make_service(num_controllers=1)
        serial = "mock_controller_0"
        # Deterministic resting noise so priming is reproducible.
        random.seed(926)

        # A controllable virtual clock so the test is fast and deterministic.
        clock = {"t": 1000.0}

        import services.controller_manager.mock_control_service as svc_mod
        import services.controller_manager.multiplexer.mock_adapter as adapter_mod

        orig_svc_time = svc_mod.time.time
        orig_adapter_time = adapter_mod.time.time
        svc_mod.time.time = lambda: clock["t"]
        adapter_mod.time.time = lambda: clock["t"]
        try:
            model = _GameSideModel(death_threshold=death_threshold)

            # Prime the EMA from rest BEFORE the death (resting-primed regime).
            # Each frame is a fresh resting poll (~1.0g noise) fed to the model.
            for _ in range(rest_prime_frames):
                clock["t"] += send_interval
                rest_frame = adapter.poll(serial)
                model.feed(rest_frame[StateKey.ACCEL], clock["t"])

            # Real production RPC: sets death_frames_remaining + the idle fallback.
            resp = service.SimulateDeath(controller_manager_mock_pb2.DeathRequest(serial=serial), None)
            assert resp.success

            poll_interval = 1.0 / poll_hz
            state_cache = adapter.poll(serial)  # discovery loop's first write
            # The send loop has its own phase: the next send lands
            # ``first_send_offset`` after the death was fired. A starved loop can
            # easily have just sent right before SimulateDeath, so its next send
            # is a full interval away — long enough that the OLD wall-clock-only
            # 1.0s hold has already reverted the cache to noise, skipping the
            # death entirely (#926).
            next_send = clock["t"] + first_send_offset
            steps = int(wall_seconds / poll_interval)
            for _ in range(steps):
                clock["t"] += poll_interval
                # DISCOVERY loop: refresh the cache at poll_hz (no budget drain).
                state_cache = adapter.poll(serial)
                # SEND loop: only fires every send_interval; it reads the LATEST
                # cached frame and drains exactly one budget frame.
                if clock["t"] >= next_send:
                    model.feed(state_cache[StateKey.ACCEL], clock["t"])
                    model.respawn_if_grace_started(clock["t"])
                    adapter.consume_death_frame(serial)
                    next_send += send_interval
            return model
        finally:
            svc_mod.time.time = orig_svc_time
            adapter_mod.time.time = orig_adapter_time

    def test_death_lands_under_stretched_send_cadence_926(self):
        """#926: a starved send loop still delivers the death.

        The send loop's next send lands ~1.25s after SimulateDeath (it had just
        sent before the death fired — typical under starvation). On the OLD code
        (wall-clock-only 1.0s hold) the discovery poll loop has already reverted
        the cache to noise by the time that send reads it, so ZERO death frames
        are streamed and the player NEVER dies — this assertion FAILS on the old
        code. On the rework the frame budget keeps poll() reporting the death
        until the send loop actually drains it, so the death is delivered no
        matter how starved the cadence, the EMA crosses, and the player dies.
        """
        model = self._simulate_in_game_death(send_interval=1.25, first_send_offset=1.25)
        assert model.kills == 1, "death must register even when the send loop is starved"

    def test_no_rekill_after_grace_under_starvation_757(self):
        """#757: after the kill, no death frame survives the 2.0s grace.

        The small frame budget exhausts at the kill, so even at a heavily
        starved send cadence no death-level frame is streamed after the grace
        window expires — the respawned player is not re-killed. The old budget-8
        fix streamed death frames for ~8 sends, outliving grace under starvation
        and re-killing on grace expiry.
        """
        model = self._simulate_in_game_death(send_interval=1.25, wall_seconds=10.0)
        assert model.kills == 1
        assert model.rekills == 0, "no #757 grace-expiry re-kill"

    def test_death_lands_at_normal_cadence(self):
        """Sanity: at the normal 60Hz send cadence the death also lands once."""
        model = self._simulate_in_game_death(send_interval=1.0 / 60.0, wall_seconds=4.0)
        assert model.kills == 1
        assert model.rekills == 0

    # Every (sensitivity, tempo) the mock can face, threshold straight from
    # base.py SLOW_MAX / FAST_MAX. The death input (~9.95g) must cross EVERY one
    # of these within the 2-frame delivery budget from a resting-primed EMA.
    # The sensitivity-3-fast (3.2g) and sensitivity-4 (3.2g slow / 3.5g fast)
    # cases are the gap the #926 re-review caught: the old 7.07g input only
    # reached 3.19g on its second delivered frame, so it could NOT cross these
    # and the death was silently dropped (the integration suite papered over it
    # because kill_player_verified re-fires until a low-threshold slow window).
    @pytest.mark.parametrize(
        ("sensitivity", "tempo", "threshold"),
        [
            (0, "slow", _SLOW_MAX[0]),
            (0, "fast", _FAST_MAX[0]),
            (1, "slow", _SLOW_MAX[1]),
            (1, "fast", _FAST_MAX[1]),
            (2, "slow", _SLOW_MAX[2]),
            (2, "fast", _FAST_MAX[2]),
            (3, "slow", _SLOW_MAX[3]),
            (3, "fast", _FAST_MAX[3]),  # 3.2g — FAILS on the old 7.07g/budget-2 code
            (4, "slow", _SLOW_MAX[4]),  # 3.2g — FAILS on the old code
            (4, "fast", _FAST_MAX[4]),  # 3.5g — highest threshold; FAILS on the old code
        ],
    )
    def test_death_crosses_every_sensitivity_and_tempo(self, sensitivity, tempo, threshold):
        """The death input crosses the death threshold at ALL sensitivities/tempos.

        Resting-primed EMA + a stretched (starved) send cadence, driven through
        the real SimulateDeath latch. With the ~9.95g death input every threshold
        in the SLOW_MAX/FAST_MAX tables is crossed within the 2 delivered death
        frames, so the player always dies and is never re-killed after grace.
        """
        model = self._simulate_in_game_death(
            send_interval=1.25,
            first_send_offset=1.25,
            wall_seconds=10.0,
            death_threshold=threshold,
        )
        assert model.kills == 1, (
            f"sensitivity {sensitivity} {tempo} (threshold {threshold}g) must die within the {2}-frame delivery budget"
        )
        assert model.rekills == 0, "no #757 grace-expiry re-kill at any sensitivity"
