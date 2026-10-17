# Controller Connectivity Troubleshooting

This guide helps diagnose and resolve PS Move controller connectivity issues in JoustMania.

## Quick Diagnostics

### Checking Signal Strength (RSSI)

**Via WebUI:**
1. Navigate to http://localhost/battery
2. Check the "Signal" column for each controller
3. Colors indicate quality:
   - **Green** = Excellent/Good (-55 dBm or better)
   - **Yellow** = Fair (-70 to -80 dBm)
   - **Red** = Poor (below -80 dBm)
   - **Gray** = USB connection

**Via Prometheus Metrics:**
```bash
# Check current RSSI for all controllers
curl localhost:8000/metrics | grep controller_rssi_dbm

# Example output:
# controller_rssi_dbm{serial="ABCD1234"} -65.0
# controller_rssi_dbm{serial="EFGH5678"} -72.0
```

**Via Controller LED Warnings:**
- **Orange pulse** (3 flashes): Weak signal warning (<-80 dBm)
- Displayed at most once per minute to avoid spam
- Move closer to Raspberry Pi when you see this

## Understanding RSSI Values

RSSI (Received Signal Strength Indicator) measures Bluetooth signal quality in dBm.

| RSSI Range | Quality | Distance | Expected Behavior |
|------------|---------|----------|-------------------|
| -40 or higher | Excellent | < 2 meters | Perfect connection, zero lag |
| -55 to -65 | Good | 2-5 meters | Smooth gameplay |
| -70 to -80 | Fair | 5-8 meters | Occasional lag possible |
| -85 or lower | Poor | > 8 meters | Frequent disconnections |
| 0 | USB | N/A | Wired connection (no RSSI) |

**What affects RSSI?**
- Distance from Raspberry Pi
- Obstacles (walls, furniture, metal objects)
- Interference (WiFi routers, microwaves, other Bluetooth devices)
- Body blocking (standing between controller and Pi)

## Common Issues & Solutions

### Issue: Controller Keeps Disconnecting

**Symptoms:**
- Controller shows as disconnected frequently
- Gameplay interrupted by connection loss
- Orange warning flashes during play

**Diagnosis:**
1. Check RSSI value in WebUI
2. If RSSI < -80 dBm, signal is too weak

**Solutions:**
- **Move closer** to Raspberry Pi (within 5 meters)
- **Remove obstacles** between controller and Pi
- **Ensure line of sight** when possible
- **Check for interference** (turn off nearby 2.4GHz devices)
- **Use an external CSR adapter** (e.g. Feasycom FSC-BP119) instead of the Pi's internal radio: the internal radio is poor for PS3 Moves (ZCM1)
- **Check the adapter chipset** with `hciconfig -a | grep Manufacturer`: Realtek (93) adapters time PS3 Moves poorly with fewer than about 5 active links; Actions, Barrot and AIC8800 adapters are poor in general ([hardware guide](hardware-setup-guide.md#chipset-beats-marketing))
- **Do not force the Peripheral role** (`hcitool sr ... peripheral`): let BlueZ negotiate; forcing Peripheral can lower link capacity and timing
- **Reposition Pi** to center of play area

### Issue: Laggy Controller Input

**Symptoms:**
- Delay between button press and response
- Movement feels sluggish
- Game feels unresponsive

**Diagnosis:**
1. Check RSSI value
2. If RSSI is between -75 and -85 dBm, bandwidth may be limited
3. Check number of connected controllers

**Solutions:**
- **Move closer** to Pi (improve signal strength)
- **Reduce controller count** if using many controllers
- **Minimize interference** (see below)
- **Check CPU load** on Pi (high CPU can cause lag)
- **Restart services** if lag persists

### Issue: Orange Flashing During Gameplay

**Meaning:** Weak signal warning (RSSI < -80 dBm)

**Immediate Action:**
- Move closer to Raspberry Pi
- Check for obstacles blocking signal
- Ensure Pi is powered properly (weak power = weak Bluetooth)

**Prevention:**
- Position Pi at center of play area
- Keep players within 5-meter radius
- Avoid corners/edges of room

### Issue: Controller Works USB but not Bluetooth

**Symptoms:**
- Controller works when plugged in (USB)
- Disconnects immediately when unplugged
- RSSI shows 0 or N/A

**Diagnosis:**
1. Controller not properly paired to Pi
2. Bluetooth adapter issue
3. Pi Bluetooth disabled

**Solutions:**
```bash
# Check Bluetooth status
sudo systemctl status bluetooth

# Enable Bluetooth if disabled
sudo systemctl enable bluetooth
sudo systemctl start bluetooth

# Re-pair controller
# Use JoustMania pairing mode or manual pairing tools
```

### Issue: Multiple Controllers, Some Work, Some Don't

**Symptoms:**
- Some controllers connect fine (good RSSI)
- Others keep disconnecting (poor RSSI)
- Physical positions are similar

**Diagnosis:**
- Bluetooth bandwidth limit reached (typically 7-8 PS Move controllers max)
- Some controllers have weaker radios
- Interference affecting specific frequencies

**Solutions:**
- **Reduce controller count** (test with fewer controllers)
- **Identify weak controllers** (check RSSI for each)
- **Replace weak controllers** (older PS Move may have degraded radios)
- **Use multiple Pis** with separate Bluetooth adapters if needed

## Interference Troubleshooting

**Common Sources of 2.4GHz Interference:**
- WiFi routers (especially on channels 1-11)
- Microwave ovens (when running)
- Cordless phones (2.4GHz models)
- Baby monitors
- Wireless speakers/headphones
- Other Bluetooth devices

**Mitigation:**
```bash
# Check WiFi channel usage
sudo iwlist wlan0 scan | grep -E 'ESSID|Channel|Frequency'

# Switch Pi WiFi to 5GHz if possible (reduces interference)
# Or use Ethernet instead of WiFi

# Disable WiFi during gameplay if not needed
sudo rfkill block wifi
```

**Best Practices:**
- Turn off microwaves during gameplay
- Keep WiFi router > 3 meters from Pi
- Use 5GHz WiFi or Ethernet for Pi networking
- Limit other Bluetooth devices in play area

## Optimal Setup Recommendations

### Ideal Configuration

**Physical Setup:**
- Raspberry Pi positioned at center of play area
- Pi elevated 1-2 meters off ground (table/shelf)
- Players move within 5-meter radius of Pi
- Clear line of sight when possible
- Away from walls/corners

**Technical Setup:**
- External CSR Bluetooth adapter (Feasycom FSC-BP119 recommended; identify the chipset with `hciconfig -a`, not by "Class 1" or range claims). The Pi 5 internal radio only suits a few PS4 Moves (ZCM2); see the [hardware guide](hardware-setup-guide.md#bluetooth-adapter-recommendations)
- Good 5V 3A power supply for Pi (weak power = weak Bluetooth)
- Pi connected via Ethernet (not WiFi 2.4GHz)
- Bluetooth keepalive tuned (already done in Phase 26)

**Player Guidelines:**
- Stay within 5 meters of Pi
- Avoid standing between Pi and other players
- Call out "weak signal" if orange pulse appears
- Charge controllers when battery < 40%

### Testing Your Setup

**Signal Strength Test:**
```bash
# 1. Connect one controller
# 2. Navigate to http://pi-address:5000/battery
# 3. Note RSSI value
# 4. Walk to different positions in play area
# 5. Refresh page and check RSSI at each position

# Expected results:
# - 1m: -40 to -50 dBm (Excellent)
# - 3m: -60 to -70 dBm (Good)
# - 5m: -70 to -80 dBm (Fair)
# - 8m+: -85+ dBm (Poor - too far!)
```

**Multi-Controller Test:**
```bash
# 1. Connect all controllers for your event
# 2. Check RSSI for each controller
# 3. Identify weakest controller
# 4. If any < -75 dBm, reposition players or Pi
# 5. Run a test game and monitor for disconnections
```

## Monitoring with Grafana

Open the **Controllers & Bluetooth** dashboard at `http://localhost/grafana/d/controller-overview`
(also embedded in the dashboard's Metrics tab):

- **Weak Signal** counts controllers below -80 dBm (`bluetooth_device_rssi_dbm < -80`), **Low Battery**
  counts battery level 0 or 1 (`controller_battery_level < 2`), and **Poor Connection** counts controllers
  whose report gaps are poor (`controller_connection_quality == 2`).
- The **Controller Health** table shows each controller's adapter, RSSI, battery, poll rate and connection
  quality; the **Connection Quality** row shows the p95 and longest report gap per controller.
- The collapsed **Bluetooth and Adapters (Pi only)** row has the signal per adapter, RSSI history, per-adapter
  poll rate and errors, adapter identity, HCI errors and link roles. RSSI, adapter identity, HCI errors and
  link roles come from the pairing daemon (only links it reports as connected are shown); the per-adapter
  poll panels join `controller_adapter_info`, which the controller manager sets from its python-hid /
  rust-hid backend (`get_adapter_for_serial`). All of them are empty in mock mode.

No alert rules ship (FR-083 Rejected, #1425): watch the dashboard.

## Advanced Diagnostics

### BlueZ RSSI Inspection

If RSSI is not showing in WebUI:

```bash
# Check BlueZ version (need 5.0+)
bluetoothctl --version

# List paired devices
bluetoothctl devices

# Connect to bluetoothctl
bluetoothctl

# Inside bluetoothctl:
> info <MAC_ADDRESS>
# Look for RSSI field

# Check if device is connected
> paired-devices
```

### Manual RSSI Query

```python
# Run from controller_manager directory
python3 << 'EOF'
from bluetooth import get_hci_dict, get_all_device_rssi_values

hci_dict = get_hci_dict()
print(f"HCI adapters: {hci_dict}")

for hci in hci_dict.keys():
    rssi_values = get_all_device_rssi_values(hci)
    print(f"{hci} RSSI values:")
    for addr, rssi in rssi_values.items():
        print(f"  {addr}: {rssi} dBm")
EOF
```

### Service Logs

Check logs for RSSI warnings:

```bash
# Controller manager logs
docker logs joustmania-controller-manager-1 | grep -i "rssi\|weak signal"

# Expected output:
# WARNING - Controller ABCD1234 has weak signal: -85 dBm
# INFO - Weak signal warning displayed for ABCD1234
# INFO - Mapped controller ABCD1234 to BT address 00:1A:2B:3C:4D:5E
```

## FAQ

**Q: Does RSSI checking affect performance?**
A: No. RSSI is checked once every 10 seconds via efficient DBus calls (~1-2ms per controller). No impact on gameplay.

**Q: Why does RSSI show 0?**
A: Either USB connection (no wireless signal) or RSSI not available (older BlueZ version, disconnected controller).

**Q: Can I disable weak signal warnings?**
A: Not currently. Warnings are rate-limited to once per minute and are important for user experience.

**Q: My controller is 2 meters away but shows poor RSSI. Why?**
A: Could be interference, obstacles, or a weak Bluetooth radio in the controller or the Pi. Try an external CSR adapter, and check the chipset of the one you have (`hciconfig -a`): RSSI can look fine while a weak chipset still delivers gappy reports.

**Q: Do all controllers have the same range?**
A: No. Older PS Move controllers may have degraded Bluetooth radios. RSSI helps identify weak units.

**Q: Does charging affect Bluetooth signal?**
A: Controllers work fine while charging via USB (wired mode, RSSI = 0). When unplugged, RSSI may initially be lower due to battery state.

## Related Documentation

- **Phase 48 Planning:** `planning/phases/completed/phase-48-controller-connection-strength-monitoring.md`
- **Controller Manager:** `services/controller_manager/README.md`
- **Proto Schema:** `proto/controller_manager.proto` (rssi field)
- **Bluetooth Module:** `services/controller_manager/bluetooth.py`
- **Metrics:** http://localhost:8000/metrics (Prometheus endpoint)
- **WebUI:** http://localhost/battery (Signal strength display)

## Getting Help

If connectivity issues persist:

1. Check RSSI values and follow solutions above
2. Review service logs for errors
3. Test with minimal setup (1 controller, close to Pi)
4. Check GitHub issues for similar problems
5. Create detailed bug report with:
   - RSSI values
   - Distance from Pi
   - Number of controllers
   - Pi model and OS version
   - Service logs

---

**Last Updated:** Phase 48 - January 2026
**Maintainer:** JoustMania Development Team
