"""The colour tables the admin handler and SettingsOps share (#2380)."""

import pytest

from lib.colors import Colors
from services.menu import settings_ops
from services.menu.handlers import admin


@pytest.mark.uc("UC-012", flow="A1")
def test_admin_uses_the_shared_colour_tables():
    assert admin.SENSITIVITY_COLORS is settings_ops.SENSITIVITY_COLORS
    assert admin.AGENT_LEVEL_COLORS is settings_ops.AGENT_LEVEL_COLORS
    assert [c.value for c in settings_ops.SENSITIVITY_COLORS] == [
        Colors.Blue.value,
        Colors.Turquoise.value,
        Colors.Green.value,
        Colors.Orange.value,
        Colors.Red.value,
    ]
    assert settings_ops.AGENT_LEVEL_COLORS["none"] is Colors.Red
