# Hardware Setup Guide

Complete guide for setting up JoustMania on Raspberry Pi 5, covering USB configuration, Bluetooth adapters, power management, and mobile deployments.

---

## Table of Contents

- [USB Port Selection](#usb-port-selection)
- [Bluetooth Adapter Recommendations](#bluetooth-adapter-recommendations) (chipset first)
- [Controller Pairing](#controller-pairing)
- [Power Configurations](#power-configurations)
- [Mobile/Portable Setups](#mobileportable-setups)
- [Scaling Guide](#scaling-guide)
- [25 Controllers on One Pi](#25-controllers-on-one-pi) (NFR-025)
- [Stage Hotspot (convention play)](#stage-hotspot-convention-play) (UC-061)
- [Physical Placement](#physical-placement)
- [Troubleshooting](#troubleshooting)

---

## USB Port Selection

### USB 3.0 Interference with Bluetooth

**Important**: USB 3.0 generates electromagnetic interference in the 2.4 GHz band - the same frequency Bluetooth uses.

| USB Port Type | Bluetooth Impact |
|---------------|------------------|
| USB 3.0 | Causes 2.4GHz interference, reduced range, dropouts |
| USB 2.0 | No interference, recommended for Bluetooth |

**Symptoms of USB 3.0 interference**:
- Reduced controller range
- Random disconnections
- Increased latency
- Controllers must be closer to adapter

### RPi 5 USB Layout

```
Raspberry Pi 5 (USB ports facing you)
┌─────────────────────────────────────────┐
│                                         │
│   [USB 3.0]  [USB 3.0]  ← Avoid for BT  │
│   [USB 2.0]  [USB 2.0]  ← Use for BT    │
│                                         │
└─────────────────────────────────────────┘
```

### Recommendation

**Always connect Bluetooth adapters to USB 2.0 ports** (directly or via hub).

If you must use USB 3.0 ports for other devices, keep them physically separated from Bluetooth adapters.

---

## Bluetooth Adapter Recommendations

### Chipset beats marketing

What decides how well an adapter handles PS Moves is its **chipset**, not the brand, the
Bluetooth version, "Class 1", "150 m range" or antenna size. Many BT 5.x "long range" dongles
are Realtek, Barrot or Actions inside, and they behave very differently with PS3 Moves (ZCM1).
These findings come from upstream JoustMania's adapter measurements (2026); we have not
re-measured them on our stack.

**Identify the chipset** by its HCI manufacturer, not by the box:

```bash
hciconfig -a | grep -E "^hci|Manufacturer"
#   hci1:   Type: Primary  Bus: USB
#           Manufacturer: Cambridge Silicon Radio (10)
```

| HCI manufacturer (id) | Chipset family | PS Move behaviour |
|---|---|---|
| Cambridge Silicon Radio (10) | CSR8510 A10 | **Recommended.** Good timing nearby at 1 to 7 links; at distance it varies by model (Cirago and Sena CSR adapters were poor at distance) |
| Realtek Semiconductor (93) | RTL8761BU (BT 5.x dongles) | PS3 Move timing poor below about 5 active links, good at 5; some samples accept only 5 links |
| Broadcom (15) | BCM20702 | Mediocre (7 PS3 Moves about 71 updates/s, p95 report gap about 32 ms) |
| Cypress (305) | BCM4345C0 (Pi 5 internal radio) | Fine for a few PS4 Moves (ZCM2) indoors; poor for PS3 Moves (see below) |
| Actions, Barrot, AICSemi (AIC8800 combo) | various | **Avoid**: poor timing |

USB id `0a12:0001` alone does not prove a real CSR chip: clones exist (the kernel carries
workarounds for them). Trust the HCI manufacturer line, then test with real controllers.

### Recommended adapter

**Feasycom FSC-BP119** (CSR8510 A10, BT 4.0, Class 1, external antenna):

- One adapter, 7 PS3 Moves nearby: about 78 updates/s per controller, p95 report gap about 25 ms.
- Two adapters, 12 PS3 Moves (6 + 6): nearby about 83 to 86 updates/s and p95 about 18 to 20 ms.
  At distance all 12 stayed connected, but timing was poor (about 48 to 53 updates/s, p95 about
  45 to 55 ms, gaps up to about 290 ms).
- Not verified yet: 14 on two adapters (7 + 7), long sessions, outdoor range.

Sena UD100 (also CSR) was similar nearby but poor at distance, and stalled when used alongside a Feasycom.

### Realtek RTL8761BU adapters

Most BT 5.x dongles sold today (TP-Link UB500 Plus, ASUS BT600, ZEXMTE, StarTech BT 5.3, GAROGYI,
Krisnorey, Hakimonoe, Techkey, ...) are Realtek RTL8761BU. They work, with caveats for PS3 Moves:

- Timing is poor with only a few active links and good from about **5 active links** per adapter.
  Spreading few controllers thinly over many Realtek adapters is the worst case.
- Some samples accept **only 5 links**, not 7; a 6th link can take about 20 s to come up.

So "5 per adapter" is Realtek advice, not a general rule: with CSR adapters 6 to 7 per adapter
had good timing nearby. The pairing daemon spreads controllers over the least-loaded adapter (UC-001 BR-001)
and does not know the chipset.

### Pi 5 internal Bluetooth

The Pi 5 radio (Cypress BCM4345C0 on UART) is fine indoors for **a few PS4 Moves (ZCM2)**, but not for
PS3 Moves (ZCM1): 1 PS3 Move reached about 59 updates/s, 7 PS3 Moves only about 6 updates/s with a
p95 gap of about 300 ms. That is why `scripts/setup/setup_host.sh` disables it (`dtoverlay=disable-bt`)
for multi-adapter rigs. A tiny PS4-Move-only setup could leave it on.

### Central / Peripheral role

Let BlueZ negotiate the link role. **Do not force the Pi to Peripheral** (for example with
`hcitool sr <addr> peripheral`): forcing Peripheral can lower link capacity and timing. On one
Realtek sample, six all-Central links were clearly better than six with one Peripheral link; an
older CSR setup was limited to about 2 links per adapter when forced to Peripheral (a historical
observation, not a current measurement).

### Judging link quality

- Use the report gap, not updates/s alone: p95 gap at most 22 ms is good, up to 30 ms is acceptable,
  above 30 ms is poor; the longest gap over 10 s should stay under 30 to 50 ms.
- A controller that "stays connected" with 500 ms gaps has failed a range test.
- Inquiry TX power is not link power or power class; the ACL MTU (`310:10`, `1021:6`) says nothing
  about capacity or quality; zero HCI error counters do not prove there were no retransmissions.

### Other features worth having

- **External antenna** (and a USB extension cable to move the adapter away from the Pi)
- **Linux support** (CSR and Realtek both work with the stock `btusb` driver)
- PS Move uses Bluetooth Classic; any BT 2.1+ adapter can talk to it, the chipset decides how well

### Controllers Per Adapter

Bluetooth Classic allows **at most 7 active links per adapter** (piconet limit). How many work
*well* depends on the chipset: CSR 6 to 7 (nearby), Realtek about 5 (some Realtek samples stop at 5).
The pairing daemon does not enforce a cap (C-011).

| Adapters | CSR (6-7 each) | Realtek (5 each) |
|----------|----------------|------------------|
| 1 | 6-7 | 5 |
| 2 | 12-14 (12 tested; poor timing at distance) | 10 |
| 3 | 18-21 | 15 |
| 4 | 24-28 | 20 |
| 6 | 36-42 | 30 |

---

## Controller Pairing

PS Move controllers must be paired to your Raspberry Pi before they can connect via Bluetooth. JoustMania includes an automatic pairing daemon that handles this process.

### How Pairing Works

1. **USB Connection**: Controller is connected via USB cable
2. **Host MAC Written**: The pairing daemon writes your Bluetooth adapter's MAC address to the controller
3. **BlueZ Trust**: The controller is marked as trusted on the adapter it was paired to (not on bluetoothctl's default adapter)
4. **Bluetooth Connection**: After unplugging USB, pressing the PS button connects via Bluetooth

### Pairing Daemon

The pairing daemon runs as a Docker container (`pairing-daemon`) and automatically detects USB-connected controllers.

**Management**:
```bash
# View logs (live)
docker compose logs -f pairing-daemon

# Restart daemon
docker compose restart pairing-daemon

# Health check (port 8002 serves /healthz and the unpair API only; metrics go out over OTLP)
curl http://localhost:8002/healthz
```

Pairing and Bluetooth metrics (`psmove_pairing_*`, `bluetooth_device_rssi_dbm`) are pushed to the
OTel collector; query them in Grafana (`/grafana/`) or see
[telemetry.md section 6](specs/telemetry.md#6-metrics).

### Pairing a Controller

1. **Plug in** the PS Move controller via USB cable
2. **Wait** for the log line `PAIRING SUCCESS: Controller <serial> paired to adapter <MAC> - unplug USB and press PS button`
   (`docker compose logs -f pairing-daemon`); `PAIRING FAILED` means it did not work and the next poll retries
3. **Unplug** the USB cable
4. **Press PS button** to connect via Bluetooth

The daemon polls USB every `pairing.poll_interval` seconds (flag in the `system` domain; default 10, variants
`fast` 5 / `normal` 10 / `slow` 30), so there may be a short delay before pairing starts.

### Pairing Multiple Controllers

You can pair multiple controllers in sequence:

1. Plug in first controller, wait for `PAIRING SUCCESS` in the log
2. Unplug first controller
3. Plug in second controller, wait for `PAIRING SUCCESS`
4. Repeat for all controllers
5. Press PS button on each to connect via Bluetooth

Re-pairing a controller the Pi already knows does not restart the Bluetooth service. A controller that is new to the selected adapter gets a BlueZ device record, and the daemon restarts the Bluetooth service so BlueZ loads it: once 20 s (`pairing.restart_quiet_period`) passed without a controller being paired or waiting on USB to be paired and no real game is starting, running or ending, otherwise right after the game. Pair a whole stack one after the other (plug in, wait for the green flash, unplug, next one) and they cost a single restart. That restart drops the controllers connected in the lobby (press PS to reconnect), but never a running game's players (`pairing.bluez_restart_mode=when_idle`, UC-001 A7, BR-005). A new controller paired during a game connects after that game. A paired controller left plugged in to charge does not hold the restart back.

While it is on USB the controller shows the pairing state on its LED (UC-001 BR-010): a slow white pulse while it waits for or goes through pairing, three green flashes when it is paired (unplug it now), steady red when pairing failed (it is tried again at the next poll, about every 10 s).

### Troubleshooting Pairing

**Controller not detected:**
```bash
# Check if daemon is running
docker compose ps pairing-daemon

# Check USB device detected
lsusb | grep Sony

# View daemon logs
docker compose logs --tail=50 pairing-daemon
```

**Pairing succeeds but Bluetooth won't connect:**
```bash
# Check if controller is trusted
bluetoothctl devices Paired

# Verify ClassicBondedOnly setting
grep ClassicBondedOnly /etc/bluetooth/input.conf
# Should show: ClassicBondedOnly=false

# Restart Bluetooth
sudo systemctl restart bluetooth
```

**First-generation PS Move controllers:**

Older PS Move controllers (without the light-up PS button) require a special Bluetooth setting:

```bash
# Edit Bluetooth config
sudo nano /etc/bluetooth/input.conf

# Ensure this line exists:
ClassicBondedOnly=false

# Restart Bluetooth
sudo systemctl restart bluetooth
```

This is automatically configured by `setup_host.sh`.

**Reset all PS Move registrations** (controllers paired to the wrong adapter, stale
registrations from an unplugged adapter):

```bash
# List what would be removed (changes nothing)
scripts/hardware/reset_bluetooth_connections.sh --dry-run

# Remove every PS Move registration, then re-pair the controllers over USB
scripts/hardware/reset_bluetooth_connections.sh
```

Only PS Moves (Sony vendor `054c`, product `03d5` / `0c5e`) are removed: live BlueZ devices,
and saved registrations under `/var/lib/bluetooth` of adapters BlueZ does not currently present
(for example unplugged ones). If bluetoothd is not running, only the saved registrations are
cleaned; if BlueZ answers with any other error (busy, access denied), nothing is removed. Keyboards, headsets and other Bluetooth devices stay paired. Each removal is reported;
the script exits non-zero if any failed.

No reboot is needed. After a real run the script restarts `pairing-daemon`, because the running
daemon remembers controllers it already verified and would not re-pair them over USB until it
restarts. If you run `tools/clear_devices.py` on the host instead, run
`docker compose restart pairing-daemon` yourself.

### Manual Pairing (Alternative)

If the daemon isn't working, you can pair manually using `bluetoothctl`:

```bash
# Connect the controller via USB, then check logs for its MAC address
docker compose logs pairing-daemon | grep "serial"

# Trust the controller on the adapter it was paired to (the daemon logs
# "paired to adapter <ADAPTER_MAC>"); a bare `bluetoothctl trust` acts on the
# default adapter, which is wrong with several adapters
bluetoothctl
  select <ADAPTER_MAC>
  trust AA:BB:CC:DD:EE:FF
  quit

# Restart Bluetooth
sudo systemctl restart bluetooth
```

Replace `AA:BB:CC:DD:EE:FF` with your controller's Bluetooth address (shown in pairing daemon logs).

---

## Power Configurations

### USB Hub Types

| Hub Type | Power Source | Current per Port | Best For |
|----------|--------------|------------------|----------|
| Bus-powered USB 2.0 | RPi USB port | ~125mA (shared 500mA) | Mobile, small setups |
| Powered USB 2.0/3.0 | Wall adapter | 500mA+ per port | Stationary, large setups |

### Bus-Powered Hub Limits

Each RPi 5 USB 2.0 port provides **500mA total**.

| Adapters per Hub | Current Draw | Status |
|------------------|--------------|--------|
| 2 adapters | ~300mA | Safe |
| 3 adapters | ~450mA | At limit |
| 4 adapters | ~600mA | Over limit - unstable |

### Power Draw Summary

| Component | Power Draw |
|-----------|------------|
| RPi 5 (idle) | 3-5W |
| RPi 5 (active, Docker services) | 8-12W |
| Each Bluetooth adapter | ~0.5W |
| 6 adapters total | ~3W |

---

## Mobile/Portable Setups

### Power Bank Requirements

For mobile JoustMania, use a USB-C PD power bank:

| Controller Count | Adapters | System Power | Recommended Battery |
|------------------|----------|--------------|---------------------|
| 14 | 2 | ~11W | 10,000+ mAh |
| 24 | 4 | ~12W | 20,000+ mAh |
| 42 | 6 | ~13W | 26,800+ mAh |

### Runtime Estimates

| Power Bank | Capacity | Runtime @ 12W |
|------------|----------|---------------|
| 10,000 mAh | ~37Wh | ~3 hours |
| 20,000 mAh | ~74Wh | ~5-6 hours |
| 26,800 mAh | ~100Wh | ~7-8 hours |

### Mobile Configuration: 14 Controllers

```
Power Bank (USB-C PD, 20,000mAh)
         │
         ▼
      RPi 5
         │
    USB 2.0 ports
     │       │
     ▼       ▼
   [BT]     [BT]    ← 2× CSR adapters

14 controllers, ~11W, 6+ hours runtime
```

### Mobile Configuration: 24 Controllers

```
Power Bank (USB-C PD, 20,000mAh+)
         │
         ▼
      RPi 5
         │
    USB 2.0 ports
     │           │
     ▼           ▼
  USB Hub     USB Hub     ← 2× bus-powered 4-port hubs
  [BT][BT]    [BT][BT]    ← 4× CSR adapters

24 controllers (7+7+7+3), ~12W, 5-6 hours runtime
```

### Mobile Configuration: 42 Controllers

```
Power Bank (USB-C PD, 26,800mAh)
         │
         ▼
      RPi 5
         │
    USB 2.0 ports
     │           │
     ▼           ▼
  USB Hub     USB Hub     ← 2× bus-powered 4-port hubs
[BT][BT][BT] [BT][BT][BT] ← 6× CSR adapters

42 controllers (7×6), ~13W, 7 hours runtime
```

**Note**: 3 adapters per hub = 450mA of 500mA limit. This is tight but functional.

**Note**: 7 per adapter in these layouts assumes CSR adapters, and 14 on two adapters is not
verified yet (12 is). With Realtek adapters plan about 5 per adapter and add adapters instead
(see [Chipset beats marketing](#chipset-beats-marketing)).

---

## Scaling Guide

### Performance with Parallel Polling

JoustMania uses parallel controller polling via `asyncio.gather()`, which reads all controllers concurrently instead of sequentially.

| Controllers | Sequential Polling | Parallel Polling | Improvement |
|-------------|-------------------|------------------|-------------|
| 4 | 12ms | 3ms | 75% faster |
| 14 | 42ms | 3-5ms | 90% faster |
| 24 | 72ms | 5-10ms | 85% faster |
| 42 | 126ms | 5-15ms | 90% faster |

This optimization is critical for large player counts - without it, 42 controllers could not keep up with the game loop. The figures are design estimates, not recorded measurements.

### Recommended Configurations by Scale

| Event Size | Controllers | Adapters | USB Setup | Power |
|------------|-------------|----------|-----------|-------|
| Small (home) | 4-7 | 1 | Direct USB 2.0 | Any |
| Medium | 8-14 | 2 | Direct USB 2.0 | 10W+ |
| Large | 15-24 | 3-4 | 2× USB hubs | 12W+ |
| Event | 25-42 | 4-6 | 2× USB hubs or powered hub | 13W+ |

### Starting the stack on the Pi

On the Pi, always use `./scripts/setup/joustmania-compose.sh up -d` (what `scripts/setup/joustmania-start.sh` and the systemd autostart run): it adds `-f docker-compose.yml`, `--env-file .env` and, when present, `--env-file .env.local` (stage tag and API keys, #1488). Never run a bare `docker compose up`: it also loads the dev `docker-compose.override.yml`, which publishes the gRPC and mock control ports on the venue network (#1319).

### Lite Deployment (No Observability)

For resource-constrained or mobile deployments, use the lite Docker Compose configuration:

```bash
docker compose -f docker-compose.lite.yml up
```

This excludes Jaeger, VictoriaMetrics, Grafana, and OTEL Collector, saving about 576MB of container memory limits (Loki and its 192m limit are gone, #2059).
It still runs `rust-hid`, the default Bluetooth backend, so the controller manager
reads real controllers and the pairing daemon pairs them through it (#1318).
python-hid is not part of the lite stack; it is opt-in on the full stack only
(see [Opting in to python-hid](#opting-in-to-python-hid)).

### Opting in to python-hid

rust-hid is the default Bluetooth backend (UC-002 BR-003). Reading real controllers through the slower
python-hid backend takes all three of (#1402):

1. the `python-hid` compose profile: `./scripts/setup/joustmania-compose.sh --profile python-hid up -d`;
2. a `backend` flag variant naming `python` (`python_rust`) in `services/flagd/controller.json`, then
   `scripts/flags.sh restart` and a controller-manager restart (`backend` is read at startup);
3. `scripts/flags.sh set controller bluetooth_backend python`, so serials and USB pairing go to python-hid.

On the Pi, set the profile in a systemd drop-in (`sudo systemctl edit joustmania`, `[Service]`
`Environment=COMPOSE_PROFILES=python-hid`), not in `.env`: `.env` is tracked, and a modified tracked file
makes `scripts/setup/joustmania-upgrade.sh` refuse to switch versions (the start itself never pulls, UC-060 BR-001). With the profile off, `scripts/setup/joustmania-start.sh` removes a stale
`joustmania-python-hid` container. Adapter and routing details:
[services.md, controller-manager "Backend layering"](specs/services.md#backend-layering).

---

## 25 Controllers on One Pi

Target for NFR-025: a Raspberry Pi 5 with 25 PS Move controllers on 4 to 5 Bluetooth adapters.
**Not measured yet:** no run with 25 controllers has been recorded; the chipset figures above come from
upstream JoustMania's adapter measurements. Controllers per adapter by chipset: see
[Controllers Per Adapter](#controllers-per-adapter) (CSR 6 to 7, Realtek about 5, never more than 7), so
25 controllers need 4 to 5 CSR adapters or 5 Realtek adapters.

### USB power

An unpowered USB 2.0 hub has 500 mA in total and each adapter draws about 100 mA, so four adapters on one hub sit
at the limit, and heavy USB traffic can push them into brownouts. Symptoms: random disconnections during a
game, report gaps over 100 ms, hub resets, controllers failing to pair.

| Option | Layout | Verdict |
|---|---|---|
| A. Powered hub | One 7-port powered hub (36 W or more), 5 adapters with 5 controllers each | Dedicated power per port, room to grow past 35 controllers. Upgrade to this if the monitoring below shows problems |
| B. Distributed unpowered hubs | Two 4-port unpowered hubs with 2 adapters each, a fifth adapter directly on the Pi | Free if you own the hubs, but still near the power limit on each hub; expect occasional disconnects under load |
| C. One unpowered 4-port hub | Four adapters on one hub | Not recommended: at the power limit and capped at 20 controllers; only for short tests |

Start with B using hubs you already have and measure before buying a powered hub. Reminder from
[USB Port Selection](#usb-port-selection): put adapters on USB 2.0 ports where you can.

### Game loop frequency

The game loop streams motion at the `game_loop.update_frequency_hz` flag of the `system` domain: `low` 15,
`medium` 30, `high` 60 (default). The coordinator constant `UPDATE_FREQUENCY = 60` in
`services/game_coordinator/games/base.py` is the same fallback. The older guide's 30 Hz figure was wrong;
the code runs 60 Hz unless you lower the flag. On a marginal USB setup (option C, or a hub that browns out)
switching to `medium` reduces USB traffic and CPU; no measured numbers exist for either setting yet.

### Testing checklist

- [ ] 5 controllers on 1 adapter: basic functionality
- [ ] 10 controllers on 2 adapters: multi-adapter routing
- [ ] 25 controllers on 5 adapters: full load
- [ ] Watch the Controller Health and Connection Quality panels for at least 5 minutes
  ([CONTROLLER_CONNECTIVITY.md](CONTROLLER_CONNECTIVITY.md#monitoring-with-grafana))
- [ ] Play a full FFA game with all players and check for disconnects during intense motion
- [ ] Check CPU on the Pi (`docker stats`) and record the run to close NFR-025

### Beyond 25

Plan one adapter per 5 (Realtek) to 6 or 7 (CSR) controllers and a powered hub: 30 controllers need 5 to 6
adapters, 50 need 8 to 10. Running several games at once multiplies the update load and requires the
powered hub.

---

## Stage Hotspot (convention play)

At a venue without a wired network the Pi opens its own password-protected
5 GHz Wi-Fi hotspot, so the Host can open the dashboard, Jaeger and Grafana
([UC-061](use_cases/UC-061.md)). It is a fallback only: it comes up when the
uplink (eth0) has had no connection for about 20 s (60 s while eth0 is still
connecting, and never in the first 45 s after boot) and goes down again 15 s
after the cable is back. There is no internet over it: the game runs in
convention play mode (the agent on the rules backend).

### Set it up (at home, with the cable in)

```bash
sudo raspi-config nonint do_wifi_country DE   # your country; reboot afterwards
sudo scripts/setup/install_hotspot.sh          # asks for the password twice
# options: --ssid "JoustMania" --channel 36|40|44|48 --uplink eth0 --psk-file FILE --pause-wifi-client
```

The setup checks the Pi first and changes nothing when a check fails: the
Wi-Fi country (set and kept for the next boot), the 5 GHz channel allowed for
an access point, `dnsmasq-base` (installed when missing), no other DNS or DHCP
server, and ufw or firewalld (it prints the exact allow rules; re-run with
`--firewall-rules-added` after adding them). Then it brings the hotspot up for
20 s as a self-test while the cable stays in: join it from a phone during those
20 s to check the password. Only when the self-test passes is the fallback armed
(`enabled=yes` in `/etc/joustmania/hotspot.conf`); a refused or interrupted
setup leaves `enabled=no`. That includes a re-run with the cable on a Pi that
was already armed: a refused re-run disarms it, so re-run until it passes.
With a Wi-Fi client up on `wlan0` the self-test refuses (it needs the radio):
re-run with `--pause-wifi-client` (or answer `y` to the prompt) and the setup
disconnects that Wi-Fi connection for the self-test and reconnects the same one
afterwards, also when the self-test fails. It refuses when your SSH session
itself runs over `wlan0`; use the cable LAN then.
A re-run over the hotspot itself never disarms it and keeps the old profile
when it is refused or your session drops.

The password: 8 to 63 characters, printable ASCII, no space and no backslash.
It is stored only in the root-owned NetworkManager profile. Re-run the setup to
change it, the SSID or the channel; over the hotspot itself your session drops
and you rejoin with the new password (a channel change needs the cable in).

Undo: `sudo scripts/setup/install_hotspot.sh --remove` (or
`sudo /usr/local/lib/joustmania/joustmania-hotspot remove` from any checkout).
After an upgrade that changes the hotspot files, `joustmania-upgrade` says
"hotspot files changed: run sudo .../install_hotspot.sh".

### At the venue (Host instructions)

1. Unplug the cable if the Pi is cabled into a network you cannot join (a
   stage or lighting switch); the hotspot comes up within about a minute.
2. Phone: **turn mobile data off** (or confirm "stay connected" when the phone
   says the network has no internet). Join the SSID with the password.
3. Open `http://10.42.0.1/` (dashboard), `/jaeger/`, `/grafana/`; SSH works on
   `10.42.0.1` too.
4. A known Wi-Fi network in range (for example a phone tether the Pi joined
   before) wins over the hotspot: switch that network off, or remove its
   profile before the show.

About 8 devices can join; the hotspot is for the Host and crew, not the
audience. It uses 5 GHz only, so it does not share the controllers' 2.4 GHz
air; check the controllers' report gaps with the hotspot on during the on-Pi
check (#2088, NFR-008).

---

## Physical Placement

### Adapter Positioning

For best coverage, position Bluetooth adapters to maximize line-of-sight to players:

```
           Players
        ⭕ ⭕ ⭕ ⭕ ⭕
       ↗           ↖
    [Ant1]       [Ant2]    ← Antennas pointed toward players
       │           │
       └─── RPi ───┘
```

### USB Extension Cables

Use 1-meter USB extension cables to:
- Position adapters at player height
- Move antennas away from RPi electrical noise
- Spread adapters for better coverage

```
RPi 5
   │
   ├──[1m USB cable]──► BT adapter (left side, elevated)
   │
   └──[1m USB cable]──► BT adapter (right side, elevated)
```

### Antenna Orientation

If using adapters with adjustable antennas:
- Point antennas **toward the center** of the play area
- Angle **slightly upward** (controllers are at hand/arm height)
- Rotate antennas to find optimal signal (test with players at max distance)

---

## Troubleshooting

### Random Controller Disconnections

| Cause | Solution |
|-------|----------|
| USB 3.0 interference | Move adapters to USB 2.0 ports |
| Hub power starvation | Use powered hub or distribute across ports |
| Too many per adapter | Check the chipset (`hciconfig -a`): Realtek about 5 per adapter, CSR 6-7 |
| Weak signal | Use a CSR adapter with an external antenna (Feasycom FSC-BP119) |

### Reduced Range / Weak Signal

| Cause | Solution |
|-------|----------|
| Weak chipset (Actions, Barrot, AIC8800, Pi internal radio for PS3 Moves) | Switch to a CSR adapter; "Class 1" / "150 m" labels do not guarantee range |
| Adapter near RPi | Use USB extension cable |
| Obstacles | Position adapters with line-of-sight |
| Interference | Move away from WiFi routers, USB 3.0 devices |

### Controllers Won't Pair

| Cause | Solution |
|-------|----------|
| Pairing daemon not running | `docker compose ps pairing-daemon` |
| USB device not detected | `lsusb \| grep Sony` - check cable |
| ClassicBondedOnly=true | Edit `/etc/bluetooth/input.conf`, set to `false` |
| Adapter limit reached | Max 7 per adapter (some Realtek: 5), add more adapters |
| Power issue | Check hub isn't over current limit |

See [Controller Pairing](#controller-pairing) for detailed troubleshooting.

### ZCM2 (PS4-era) Controllers Won't Connect After Pairing

PS Move controllers come in two hardware revisions:

| Model | USB Connector | Product ID | Pairing |
|-------|--------------|------------|---------|
| ZCM1 (PS3-era) | Mini USB | 0x03d5 | Automatic via daemon |
| ZCM2 (PS4-era) | Micro USB | 0x0c5e | Requires manual step |

> **Fallback only, until hardware verification (#1465).** Since #1492 the pairing
> daemon is BlueZ's default pairing agent and answers the PIN (`0000`) and
> confirmation requests itself, for controllers it paired over USB in its current
> session (UC-001 BR-009). Use the manual steps below only if a ZCM2 still does not
> connect after `PAIRING SUCCESS`. Running `default-agent` in `bluetoothctl` takes the
> default-agent role from the daemon for as long as that bluetoothctl runs; quit it
> when done.

ZCM2 controllers need a PIN-based Bluetooth pairing handshake. If the daemon's
agent did not complete it, pair manually via `bluetoothctl` after the daemon
reports `PAIRING SUCCESS`:

```bash
# 1. Unplug the controller from USB

# 2. Start bluetoothctl with a PIN agent, on the adapter the daemon paired it to
bluetoothctl
  agent KeyboardOnly
  default-agent
  select <ADAPTER_MAC>

# 3. Press the PS button on the controller, then quickly run:
  trust <MAC_ADDRESS>
  pair <MAC_ADDRESS>

# 4. When prompted for PIN, enter: 0000

# 5. Exit bluetoothctl
  quit
```

Replace `<MAC_ADDRESS>` with the controller's address and `<ADAPTER_MAC>`
with the adapter address, both shown in the pairing daemon's
`PAIRING SUCCESS` log line (e.g. `DC:0C:2D:09:4D:7B`).

You only need to do this once per ZCM2 controller. After the initial
manual pairing, the controller will reconnect automatically when you
press the PS button.

**Tip**: You can identify your controller model by the USB connector —
mini USB (wider, trapezoidal) is ZCM1, micro USB (smaller, rectangular)
is ZCM2.

### Controllers Not Detected After Container Start (Hot-Plug)

The Docker container-manager requires specific configuration for controller hot-plug:

| Symptom | Cause | Solution |
|---------|-------|----------|
| New controllers not detected | `/dev` bind mount is snapshot | Ensure `/dev:/dev:rslave` mount |
| PSMove invalid handle errors | Device not visible in container | Add `pid: "host"` to compose |
| Works after restart only | Hot-plug not configured | Apply both fixes below |

**Required docker-compose.yml settings for hot-plug:**

```yaml
controller-manager:
  privileged: true
  pid: "host"                    # Required for device visibility
  volumes:
    - /dev:/dev:rslave           # rslave propagation for hot-plug
```

Both settings are required:
- **`pid: "host"`**: Shares host PID namespace for device event visibility
- **`/dev:/dev:rslave`**: Recursive slave mount propagates new devices into container

These are already configured in the default `docker-compose.yml`.

### Docker Memory Limit Warning

**Symptom:** `Your kernel does not support memory limit capabilities or the cgroup is not mounted. Limitation discarded.`

This means Docker's per-service memory limits (from `docker-compose.yml`) are being silently ignored because cgroup memory support isn't enabled in the kernel.

**Fix:** Enable cgroups in the kernel boot parameters:

```bash
# Raspberry Pi OS Bookworm (Debian 12+):
sudo sed -i 's/$/ cgroup_memory=1 cgroup_enable=memory/' /boot/firmware/cmdline.txt

# Raspberry Pi OS Bullseye (Debian 11):
sudo sed -i 's/$/ cgroup_memory=1 cgroup_enable=memory/' /boot/cmdline.txt
```

Then reboot:
```bash
sudo reboot
```

Verify after reboot:
```bash
docker info | grep -i cgroup
```

**Note:** On 8GB Pi models, memory limits are less critical since there's ample headroom. The system will run fine without them — you'll just see the warning.

### High Latency

| Cause | Solution |
|-------|----------|
| Sequential polling | Already uses parallel polling |
| Too many controllers | Add adapters (reduce per-adapter load) |
| USB bandwidth | Use USB 3.0 hub for hub, adapters on 2.0 |

---

## Hardware Shopping List

### Minimum (14 Controllers, Mobile)

- [ ] Raspberry Pi 5 (4GB+)
- [ ] 2× CSR Bluetooth adapters (e.g. Feasycom FSC-BP119) (dual antenna recommended)
- [ ] USB-C PD power bank (20,000mAh+)
- [ ] 2× 1m USB extension cables (optional but recommended)

### Medium (24 Controllers, Mobile)

- [ ] Raspberry Pi 5 (4GB+)
- [ ] 4× CSR Bluetooth adapters (e.g. Feasycom FSC-BP119)
- [ ] 2× USB 2.0 hubs (4-port, bus-powered OK)
- [ ] USB-C PD power bank (20,000mAh+)

### Large (42 Controllers, Mobile)

- [ ] Raspberry Pi 5 (8GB recommended)
- [ ] 6× CSR Bluetooth adapters (e.g. Feasycom FSC-BP119)
- [ ] 2× USB 2.0 hubs (4-port, bus-powered)
- [ ] USB-C PD power bank (26,800mAh+)

### Event (42+ Controllers, Stationary)

- [ ] Raspberry Pi 5 (8GB)
- [ ] 6-8× Bluetooth adapters
- [ ] Powered USB 3.0 hub (7-10 port)
- [ ] Wall power

---

## Summary

| Goal | Key Decisions |
|------|---------------|
| **Best signal** | CSR adapters (Feasycom FSC-BP119) + USB extensions + antenna positioning |
| **Mobile friendly** | USB 2.0 bus-powered hubs + 20,000mAh+ power bank |
| **No interference** | Bluetooth on USB 2.0 only, avoid USB 3.0 |
| **Scale to 42** | 6 adapters + parallel polling + lite deployment |
