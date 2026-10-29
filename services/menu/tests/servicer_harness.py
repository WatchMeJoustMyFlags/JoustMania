"""Shared test harness: a real MenuServicer with only its I/O edges autospec'd (#1358)."""

import asyncio
import contextlib
from unittest.mock import create_autospec, patch

import grpc
from openfeature.client import OpenFeatureClient

from lib.flag_service_writer import FlagServiceWriter


def default_returning_flag_client(_domain: str):
    """An OpenFeature client spec whose typed getters return the caller's default.

    Stands in for a flagd connection: every flag resolves to the default the
    production code passes, exactly as with flagd unreachable.
    """
    client = create_autospec(OpenFeatureClient, instance=True)
    for getter in (
        client.get_boolean_value,
        client.get_string_value,
        client.get_integer_value,
        client.get_float_value,
        client.get_object_value,
    ):
        getter.side_effect = lambda _key, default, *_args, **_kwargs: default
    return client


def make_real_servicer(writer_factory=None):
    """A real MenuServicer with real StateManager and handlers.

    Only the I/O edges are replaced, each by an autospec of the real class:
    gRPC channels, flagd clients/writers, LED, audio, the event publisher,
    the controller event loop and the idle monitor. ``writer_factory`` builds the
    flag-service writers instead (called with the production arguments), for
    tests that need real writers over a fake transport.
    """
    if writer_factory is None:

        def writer_factory(*_args, **_kwargs):
            return create_autospec(FlagServiceWriter, instance=True)

    channel = create_autospec(grpc.aio.Channel, instance=True)
    with (
        patch("lib.grpc_utils.create_channel", return_value=channel),
        patch("services.menu.menu_setup.init_flag_domain", autospec=True),
        patch("services.menu.menu_setup.get_flag_client", side_effect=default_returning_flag_client),
        patch("services.menu.menu_setup.FlagServiceWriter", side_effect=writer_factory),
        patch("services.menu.menu_setup.LedController", autospec=True),
        patch("services.menu.menu_setup.AudioHelper", autospec=True),
        patch("services.menu.menu_setup.EventPublisher", autospec=True),
        patch("services.menu.menu_setup.ControllerEventLoop", autospec=True),
        patch("services.menu.menu_setup.IdleMonitor", autospec=True),
    ):
        from services.menu.servicer import MenuServicer

        return MenuServicer()


async def cancel_background_tasks(servicer) -> None:
    """Cancel the background tasks the real code may have scheduled."""
    for task in (
        servicer._game_lifecycle_task,
        servicer.admin_handler._timeout_task,
        servicer.settings_ops.operator_refresh_task,
    ):
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
