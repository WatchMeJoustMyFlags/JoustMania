"""BlueZ device record for a PS Move BlueZ does not know yet (UC-001 A7, A8, BR-008; #1466).

BlueZ refuses an incoming HID connection from a device it does not know, and a
PS Move paired over USB never pairs over Bluetooth, so nothing creates that
device. Before #610, psmoveapi's Linux pairing wrote the record below and
restarted bluetoothd; this module writes the same files so bluetoothd loads
them at its next start (the restart is ``bluez_reload``'s job):

- ``/var/lib/bluetooth/<adapter>/<controller>/info``: trusted, HID service,
  ``[DeviceID]`` with the USB product id;
- ``/var/lib/bluetooth/<adapter>/cache/<controller>``: the PS Move's HID SDP
  record, which BlueZ needs to set up the HID connection because the
  controller is never queried over Bluetooth before it connects.

Existing files are never overwritten: they may hold BlueZ's own pairing data
(e.g. a ZCM2 link key).

The SDP record and the record layout come from PS Move API
(https://github.com/thp/psmoveapi, src/platform/psmove_port_linux.cpp, whose
header reads "Copyright (c) 2016, 2017 Thomas Perl <m@thp.io>" and
"Copyright (c) 2019, 2020 Alexander Nitsch <nitsch@ht.tu-berlin.de>"),
licensed as follows (verbatim from psmoveapi's COPYING):

Copyright (c) 2011, 2012 Thomas Perl <m@thp.io>
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

   1. Redistributions of source code must retain the above copyright
      notice, this list of conditions and the following disclaimer.

   2. Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
"""

from __future__ import annotations

import configparser
import contextlib
import enum
import logging
import os
import tempfile
from pathlib import Path

from lib.psmove_hid import ALL_PRODUCT_IDS, VENDOR_ID

from .registrations import DEFAULT_BLUETOOTH_DIR, normalize_address

logger = logging.getLogger("psmove-pairing")

HID_SERVICE_UUID = "00001124-0000-1000-8000-00805f9b34fb"

# The PS Move's HID SDP record (service handle 0x00010000), from psmoveapi.
HID_SDP_RECORD = (
    "3601920900000A000100000900013503191124090004350D"
    "35061901000900113503190011090006350909656E09006A09010009000"
    "93508350619112409010009000D350F350D350619010009001335031900"
    "110901002513576972656C65737320436F6E74726F6C6C6572090101251"
    "3576972656C65737320436F6E74726F6C6C6572090102251B536F6E7920"
    "436F6D707574657220456E7465727461696E6D656E74090200090100090"
    "2010901000902020800090203082109020428010902052801090206359A"
    "35980822259405010904A101A102850175089501150026FF00810375019"
    "513150025013500450105091901291381027501950D0600FF8103150026"
    "FF0005010901A10075089504350046FF0009300931093209358102C0050"
    "175089527090181027508953009019102750895300901B102C0A1028502"
    "750895300901B102C0A10285EE750895300901B102C0A10285EF7508953"
    "00901B102C0C00902073508350609040909010009020828000902092801"
    "09020A280109020B09010009020C093E8009020D280009020E2800"
)


class RecordOutcome(enum.Enum):
    """Result of write_device_record (span attribute ``pairing.bluez_record``)."""

    WRITTEN = "written"  # new record on disk, bluetoothd must load it
    EXISTS = "exists"  # a record is on disk already but BlueZ has not loaded it
    UNSUPPORTED = "unsupported"  # not a PS Move product id (A8)
    NO_ADAPTER_STORAGE = "no_adapter_storage"  # BlueZ keeps no directory for the adapter (A8)
    FAILED = "failed"  # bad address or write error (A8)

    @property
    def needs_reload(self) -> bool:
        """Whether bluetoothd has to restart to load a record (UC-001 BR-005)."""
        return self in (RecordOutcome.WRITTEN, RecordOutcome.EXISTS)


def _info_contents(product_id: int) -> str:
    return (
        "[General]\n"
        "Name=Motion Controller\n"
        "Class=0x002508\n"
        "SupportedTechnologies=BR/EDR;\n"
        "Trusted=true\n"
        "Blocked=false\n"
        f"Services={HID_SERVICE_UUID};\n"
        "\n"
        "[DeviceID]\n"
        "Source=1\n"
        f"Vendor={VENDOR_ID}\n"
        f"Product={product_id}\n"
        "Version=1\n"
    )


def _cache_contents() -> str:
    return f"[General]\nName=Motion Controller\n\n[ServiceRecords]\n0x00010000={HID_SDP_RECORD}\n"


def _create_private(path: Path, contents: str) -> bool:
    """Create ``path`` (mode 0600) atomically; False (untouched) when it already exists.

    Written to a temporary file in the same directory, fsynced, then hard-linked
    to its name (``os.link`` fails if the name exists, so an existing file is
    never replaced). The temporary file is always removed: a failure leaves no
    partial file behind.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:  # mkstemp creates it with mode 0600
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, path)
        except FileExistsError:
            return False
        return True
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def _is_valid_info(path: Path) -> bool:
    """Whether an existing ``info`` file is something bluetoothd can load (a [General] section)."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        if path.stat().st_size == 0:
            return False
        parser.read(path)
    except (OSError, configparser.Error):
        return False
    return parser.has_section("General")


def write_device_record(
    adapter_address: str,
    controller_address: str,
    product_id: int,
    bluetooth_dir: Path = DEFAULT_BLUETOOTH_DIR,
) -> RecordOutcome:
    """Write the BlueZ record of a PS Move on ``adapter_address`` (UC-001 A7, BR-008).

    Never raises; failures come back as an A8 outcome and are logged. An
    existing ``info`` file is kept; if it is empty or unreadable the outcome is
    FAILED (no restart is asked for a record bluetoothd cannot load).
    """
    adapter = normalize_address(adapter_address)
    controller = normalize_address(controller_address)
    if adapter is None or controller is None:
        logger.error(f"No BlueZ record: not Bluetooth addresses: {adapter_address!r}, {controller_address!r}")
        return RecordOutcome.FAILED
    if product_id not in ALL_PRODUCT_IDS:
        logger.error(f"No BlueZ record for {controller}: product id {product_id:#06x} is not a PS Move")
        return RecordOutcome.UNSUPPORTED

    adapter_dir = bluetooth_dir / adapter
    if not adapter_dir.is_dir():
        logger.error(f"No BlueZ record for {controller}: BlueZ keeps no storage for adapter {adapter}")
        return RecordOutcome.NO_ADAPTER_STORAGE

    device_dir = adapter_dir / controller
    info = device_dir / "info"
    created_dir = written = False
    try:
        if not device_dir.is_dir():
            device_dir.mkdir(mode=0o700)
            created_dir = True
        written = _create_private(info, _info_contents(product_id))
        cache_dir = adapter_dir / "cache"
        cache_dir.mkdir(mode=0o700, exist_ok=True)
        _create_private(cache_dir / controller, _cache_contents())
    except OSError as e:
        logger.error(f"Cannot write the BlueZ record of {controller} on {adapter}: {e}")
        _remove_if_ours(device_dir if created_dir else None, info if written else None)
        return RecordOutcome.FAILED

    if not written:
        if not _is_valid_info(info):
            # Never overwritten (it may be BlueZ's own); the controller stays blocked until it is gone (#1490)
            logger.error(
                f"BlueZ record of {controller} on {adapter} exists but is empty or unreadable: {info}. "
                f"Delete that file (and {adapter_dir / 'cache' / controller}, if it exists), "
                "then unplug the controller and plug it in again to pair it anew"
            )
            return RecordOutcome.FAILED
        logger.info(f"BlueZ record of {controller} on {adapter} exists already; kept as is")
        return RecordOutcome.EXISTS
    logger.info(f"Wrote the BlueZ record of {controller} on {adapter} (product {product_id:#06x})")
    return RecordOutcome.WRITTEN


def _remove_if_ours(device_dir: Path | None, info: Path | None) -> None:
    """Undo what this call created (its info file, its device directory): nothing partial stays."""
    with contextlib.suppress(OSError):
        if info is not None:
            info.unlink(missing_ok=True)
        if device_dir is not None:
            device_dir.rmdir()
