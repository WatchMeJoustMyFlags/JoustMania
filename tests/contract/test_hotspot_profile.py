"""The stage hotspot profile, unit, timer and keyfile rendering (UC-061, #2099).

Static checks over ``scripts/setup/joustmania-hotspot.*`` and the dispatcher hook: the
NetworkManager profile is a 5 GHz WPA2-PSK (CCMP only) access point on wlan0 that never
autoconnects and never becomes the default route (BR-002, BR-003, C-043, C-044); the unit
has no start limit, a bounded start and no ordering against the game unit (A6, BR-006);
the timer re-checks every 60 s (A3, A7). ``render`` is run for real and its output is
parsed as a GKeyFile, the format NetworkManager reads, so a password with ``&``, ``#``,
``;``, ``$`` or quotes round-trips exactly (BR-004; bash 5.2 ``patsub_replacement``).
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import subprocess

import infra_config as ic
import pytest

SETUP = ic.REPO_ROOT / "scripts" / "setup"
TEMPLATE = SETUP / "joustmania-hotspot.nmconnection.template"
HELPER = SETUP / "joustmania-hotspot.sh"
HOOK = SETUP / "90-joustmania-hotspot"
UNIT = SETUP / "joustmania-hotspot.service"
TIMER = SETUP / "joustmania-hotspot.timer"
GAME_UNIT = SETUP / "joustmania.service"
INSTALLED_HELPER = "/usr/local/lib/joustmania/joustmania-hotspot"
NON_DFS_5GHZ = {"36", "40", "44", "48"}

HOSTILE_PASSWORDS = [
    "Joust&Mania1",
    "&&&&&&&&",
    "p@PSK@ss&w0rd",
    "a#b;c$d\"e'f!",
    "`id`$(id)%s&1|<>",
    "~!@#$%^&*()_+{}[]:;?/.,=-",
    "x" * 63,
    "12345678",
]


def _ini(path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(path.read_text())
    return parser


def _code(path) -> str:
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def _helper_uuid() -> str:
    match = re.search(r"^readonly UUID=([0-9a-f-]{36})$", HELPER.read_text(), re.M)
    assert match, "the helper declares no fixed profile UUID"
    return match.group(1)


@pytest.mark.uc("UC-061", br=["BR-003"])
def test_profile_is_a_5ghz_access_point_on_wlan0_that_never_autoconnects():
    profile = _ini(TEMPLATE)
    assert profile["connection"]["id"] == "joustmania-hotspot"
    assert profile["connection"]["type"] == "wifi"
    assert profile["connection"]["interface-name"] == "wlan0"
    assert profile["connection"]["autoconnect"] == "false"
    assert profile["connection"]["uuid"] == _helper_uuid()
    assert profile["wifi"]["mode"] == "ap"
    assert profile["wifi"]["band"] == "a"
    assert profile["wifi"]["channel"] == "@CHANNEL@"
    assert profile["wifi"]["ssid"] == "@SSID@"
    assert profile["wifi"]["powersave"] == "2"


@pytest.mark.uc("UC-061", br=["BR-004"])
def test_profile_is_wpa2_psk_ccmp_only_with_pmf_explicit_and_no_password():
    sec = _ini(TEMPLATE)["wifi-security"]
    assert sec["key-mgmt"] == "wpa-psk"
    assert sec["proto"] == "rsn"
    assert sec["pairwise"] == "ccmp"
    assert sec["group"] == "ccmp"
    assert sec["pmf"] in {"1", "2", "3"}, "pmf must be set explicitly (C-043)"
    assert sec["psk"] == "@PSK@"


@pytest.mark.uc("UC-061", br=["BR-002"])
def test_profile_shares_10_42_0_1_without_a_default_route_or_ipv6():
    profile = _ini(TEMPLATE)
    assert profile["ipv4"]["method"] == "shared"
    assert profile["ipv4"]["address1"] == "10.42.0.1/24"
    assert profile["ipv4"]["never-default"] == "true"
    assert profile["ipv6"]["method"] == "disabled"


@pytest.mark.uc("UC-061", br=["BR-003"])
def test_helper_allows_only_non_dfs_5ghz_channels_and_defaults_to_one():
    text = HELPER.read_text()
    default = re.search(r"\bC_CHANNEL=(\d+)", text)
    assert default and default.group(1) in NON_DFS_5GHZ
    allowed = re.search(
        r"valid_channel\(\) \{ \[\[ \"\$1\" =~ \^\(([0-9|]+)\)\$ \]\]; \}", text
    )
    assert allowed, "valid_channel no longer has the expected form"
    assert set(allowed.group(1).split("|")) == NON_DFS_5GHZ


@pytest.mark.uc("UC-061", flow="A6", br=["BR-006"])
def test_unit_has_no_start_limit_a_bounded_start_and_runs_the_root_owned_copy():
    unit = _ini(UNIT)
    assert unit["Unit"]["StartLimitIntervalSec"] == "0"
    assert unit["Service"]["Type"] == "oneshot"
    assert unit["Service"]["ExecStart"] == f"{INSTALLED_HELPER} evaluate"
    assert 180 < int(unit["Service"]["TimeoutStartSec"]) <= 300
    assert unit["Service"]["KillMode"] == "mixed"
    assert "NetworkManager.service" in unit["Unit"]["After"]


@pytest.mark.uc("UC-061", flow="A3", br=["BR-006"])
def test_unit_is_started_only_by_the_timer_and_the_hook_never_by_a_boot_target():
    # A oneshot WantedBy=multi-user.target would hold boot until the check ends
    # (minutes while it waits); the timer's OnBootSec starts the first check
    assert not _ini(UNIT).has_section("Install")
    assert "WantedBy" not in _code(UNIT)
    assert _ini(TIMER)["Install"]["WantedBy"] == "timers.target"


@pytest.mark.uc("UC-061", br=["BR-006"])
def test_hotspot_and_game_units_never_reference_each_other():
    assert "joustmania.service" not in _code(UNIT)
    assert "joustmania.service" not in _code(TIMER)
    assert "hotspot" not in _code(GAME_UNIT)


@pytest.mark.uc("UC-061", flow="A7")
def test_timer_rechecks_every_60s_from_boot():
    timer = _ini(TIMER)["Timer"]
    assert timer["OnUnitActiveSec"] == "60s"
    assert timer["OnBootSec"] == "30s"
    assert _ini(TIMER)["Install"]["WantedBy"] == "timers.target"


@pytest.mark.uc("UC-061", flow="A6")
def test_dispatcher_hook_only_starts_never_restarts():
    code = _code(HOOK)
    assert "systemctl start --no-block joustmania-hotspot.service" in code
    assert "restart" not in code


@pytest.mark.uc("UC-061", flow="A6", br=["BR-006"])
def test_units_verify_when_systemd_analyze_is_available(tmp_path):
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze not installed")
    unit = tmp_path / "joustmania-hotspot.service"
    unit.write_text(UNIT.read_text().replace(INSTALLED_HELPER, str(HELPER)))
    timer = tmp_path / "joustmania-hotspot.timer"
    timer.write_text(TIMER.read_text())
    result = subprocess.run(
        ["systemd-analyze", "verify", str(unit), str(timer)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    problems = [
        line for line in result.stderr.splitlines() if "joustmania-hotspot" in line
    ]
    assert not problems, "\n".join(problems)


@pytest.mark.uc("UC-061", br=["BR-004"])
def test_no_tracked_keyfile_holds_a_password():
    tracked = subprocess.run(
        ["git", "-C", str(ic.REPO_ROOT), "ls-files", "*.nmconnection*"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert tracked == ["scripts/setup/joustmania-hotspot.nmconnection.template"]
    for path in tracked:
        for line in (ic.REPO_ROOT / path).read_text().splitlines():
            if line.startswith("psk="):
                assert line == "psk=@PSK@", f"{path} holds a password"
    assert "*.nmconnection" in (ic.REPO_ROOT / ".gitignore").read_text().splitlines()


def _render(
    tmp_path, name: str, password: str, ssid: str = "Joust Mania", channel: str = "44"
):
    out = tmp_path / f"{name}.nmconnection"
    result = subprocess.run(
        ["bash", str(HELPER), "render", str(TEMPLATE), str(out), ssid, channel],
        input=password + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert password not in result.stdout + result.stderr, "render printed the password"
    return result, out


_GLIB_READER = """
import json, sys
import gi
gi.require_version("GLib", "2.0")
from gi.repository import GLib
out = []
for path in sys.argv[1:]:
    kf = GLib.KeyFile()
    kf.load_from_file(path, GLib.KeyFileFlags.NONE)
    out.append([kf.get_string("wifi-security", "psk"), kf.get_string("wifi", "ssid"),
                kf.get_integer("wifi", "channel"), kf.get_string("connection", "uuid")])
print(json.dumps(out))
"""


def _glib_python() -> str | None:
    """A system python3 with PyGObject: GLib's own GKeyFile parser (what NM uses)."""
    for candidate in ("/usr/bin/python3", shutil.which("python3")):
        if not candidate:
            continue
        probe = subprocess.run(
            [candidate, "-I", "-c", "import gi; gi.require_version('GLib', '2.0')"],
            capture_output=True,
            check=False,
            timeout=30,
        )
        if probe.returncode == 0:
            return candidate
    return None


def _gkeyfile_string(path, group: str, key: str) -> str:
    """GKeyFile string semantics without GLib: leading blanks of a value are dropped
    and \\s \\n \\t \\r \\\\ are escapes (g_key_file_parse_value_as_string)."""
    current = None
    for raw in path.read_text().splitlines():
        if raw.startswith("#") or not raw.strip():
            continue
        if raw.startswith("[") and raw.endswith("]"):
            current = raw[1:-1]
            continue
        name, _, value = raw.partition("=")
        if current == group and name.strip() == key:
            value = value.lstrip(" \t")
            escapes = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
            return re.sub(
                r"\\(.)", lambda m: escapes.get(m.group(1), m.group(0)), value
            )
    raise KeyError(f"[{group}] {key}")


def _rendered_hostile(tmp_path) -> list:
    rendered = []
    for i, password in enumerate(HOSTILE_PASSWORDS):
        result, out = _render(tmp_path, f"ok{i}", password)
        assert result.returncode == 0, result.stdout + result.stderr
        assert oct(out.stat().st_mode & 0o777) == "0o600"
        rendered.append(out)
    return rendered


def _assert_round_trip(parsed) -> None:
    for password, (psk, ssid, channel, uuid) in zip(
        HOSTILE_PASSWORDS, parsed, strict=True
    ):
        assert psk == password
        assert ssid == "Joust Mania"
        assert channel == 44
        assert uuid == _helper_uuid()


@pytest.mark.uc("UC-061", br=["BR-004"])
def test_render_round_trips_hostile_passwords_through_glib_gkeyfile(tmp_path):
    """GLib's own parser, the one NetworkManager uses. CI installs python3-gi for the
    Telemetry Contract job, so there a missing GLib is a failure, not a skip."""
    python = _glib_python()
    if python is None:
        if os.environ.get("CI"):
            pytest.fail(
                "CI must run the GKeyFile round trip with GLib: install python3-gi"
            )
        pytest.skip(
            "no system python3 with PyGObject (python3-gi); the GKeyFile-rules test still runs"
        )
    rendered = _rendered_hostile(tmp_path)
    parsed = json.loads(
        subprocess.run(
            [python, "-I", "-c", _GLIB_READER, *map(str, rendered)],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
    )
    _assert_round_trip(parsed)


@pytest.mark.uc("UC-061", br=["BR-004"])
def test_render_round_trips_hostile_passwords_through_gkeyfile_rules(tmp_path):
    """The same round trip with a GKeyFile-rules reader, so it runs without GLib too."""
    rendered = _rendered_hostile(tmp_path)
    _assert_round_trip(
        [
            [
                _gkeyfile_string(p, "wifi-security", "psk"),
                _gkeyfile_string(p, "wifi", "ssid"),
                int(_gkeyfile_string(p, "wifi", "channel")),
                _gkeyfile_string(p, "connection", "uuid"),
            ]
            for p in rendered
        ]
    )


@pytest.mark.uc("UC-061", br=["BR-004"])
@pytest.mark.parametrize(
    "password",
    [
        "short7!",
        "y" * 64,
        "with space1",
        "back\\slash1",
        "tab\there1",
        "pässwort12",
        " lead1234",
    ],
)
def test_render_refuses_a_password_gkeyfile_would_not_store_verbatim(
    tmp_path, password
):
    result, out = _render(tmp_path, "bad", password)
    assert result.returncode != 0
    assert not out.exists()
