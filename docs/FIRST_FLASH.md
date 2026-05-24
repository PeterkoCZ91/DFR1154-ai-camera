# First Flash Guide — DFR1154 AI Camera

Step-by-step from unboxing to a working camera in ~15 minutes.
No prior ESP32 experience required.

---

## What you need

- DFRobot FireBeetle 2 ESP32-S3 (DFR1154) board
- USB-C cable **with data lines** (charging-only cables will not work)
- 5 V / 1 A+ power supply (USB-C or external)
- A Windows, macOS, or Linux computer
- The board's WiFi network (2.4 GHz only — 5 GHz will not work)

---

## Step 1 — Install PlatformIO

PlatformIO is the build and flash tool. The easiest install is via the VS Code extension.

**Option A — VS Code (recommended)**

1. Install [VS Code](https://code.visualstudio.com/)
2. Open Extensions (`Ctrl+Shift+X`), search **PlatformIO IDE**, install it
3. Restart VS Code — a PlatformIO icon appears in the left sidebar

**Option B — CLI only**

```bash
pip install platformio
```

> On Linux you may need `pip3` and to add `~/.local/bin` to your PATH.

---

## Step 2 — Clone and build

```bash
git clone https://github.com/PeterkoCZ91/DFR1154-ai-camera.git
cd DFR1154-ai-camera/firmware
pio run
```

First build downloads the ESP32-S3 toolchain and libraries (~500 MB, one-time).
Subsequent builds take ~30 seconds.

**Expected output at the end:**

```
Building .pio/build/esp32-s3-devkitc-1/firmware.bin
RAM:   [=         ]  20.6% (used 67584 bytes from 327680 bytes)
Flash: [===       ]  25.4% (used 1671520 bytes from 6586368 bytes)
========================= [SUCCESS] =========================
```

If you see `[ERROR]` or `[FAILED]`, see [Troubleshooting](#troubleshooting) below.

---

## Step 3 — Connect the board and find the port

Connect the DFR1154 to your computer via USB-C.

**Find the serial port:**

| OS | Command | Typical result |
|----|---------|----------------|
| Linux | `ls /dev/ttyUSB* /dev/ttyACM*` | `/dev/ttyACM0` |
| macOS | `ls /dev/cu.*` | `/dev/cu.usbmodem101` |
| Windows | Device Manager → Ports (COM & LPT) | `COM3` |

> **Linux only:** if the port exists but flash fails with "Permission denied", run:
> ```bash
> sudo usermod -aG dialout $USER
> ```
> Log out and back in, then retry.

> **No port appears at all?** The USB cable is charge-only. Try a different cable.

---

## Step 4 — Flash

```bash
cd DFR1154-ai-camera/firmware
pio run --target upload
```

PlatformIO auto-detects the port. If detection fails, specify it manually:

```bash
pio run --target upload --upload-port /dev/ttyACM0   # Linux/macOS
pio run --target upload --upload-port COM3           # Windows
```

**What you'll see:**

```
Connecting...
Chip is ESP32-S3 (revision v0.2)
Features: WiFi, BLE, Embedded Flash 16MB (XMC), Embedded PSRAM 8MB (AP_3v3)
Uploading stub...
Changing baud rate to 921600
Uploading image...
[=====>                                           ] 13% ...
[==================================================] 100%
Hash of data verified.
Leaving...
Hard resetting via RTS pin...
```

Total upload time: ~20–30 seconds.

> **"A fatal error occurred: Failed to connect"** — hold the **BOOT button** on the board
> while the tool prints `Connecting...`, then release. This forces download mode.

---

## Step 5 — First boot: find the IP address

Open the serial monitor immediately after flash:

```bash
pio device monitor
```

(baud rate 115200 is set automatically)

Watch for these lines — they appear within 10 seconds:

```
[WIFI] Connecting to <your_ssid>...
[WIFI] Connected! Local IP: http://192.168.x.x
[CAMERA] OV3660 init OK
[HTTP] Servers started on ports 80, 81, 82
```

**No WiFi credentials yet?** The camera falls back to AP mode:

```
[WIFI] No credentials — starting AP: ESP32-Camera-Setup
[AP] Connect to WiFi: ESP32-Camera-Setup (password: 12345678)
[AP] Open: http://192.168.4.1
```

Connect your phone/laptop to `ESP32-Camera-Setup`, open `http://192.168.4.1`,
enter your WiFi credentials, click Save. The board reboots and connects.

> **mDNS alternative:** If your network supports it, the camera also responds at
> `http://ESP32-Camera.local/` — no IP lookup needed.

---

## Step 6 — Verify the camera is working

Open the dashboard in a browser:

```
http://<device-ip>/
```

Default credentials: **admin / admin**

**Change them immediately** — Settings → Credentials → update username and password.

### Quick health check via curl

```bash
curl -u admin:admin http://<device-ip>/health
curl -u admin:admin http://<device-ip>/status
```

Expected `/health` response (abbreviated):

```json
{
  "wifi_connected": true,
  "camera_ok": true,
  "psram_ok": true,
  "lux_sensor_ok": true,
  "free_heap": 95432,
  "uptime_s": 42
}
```

If `camera_ok` is `false`, check the flex cable — it unplugs easily.

### Grab a snapshot

```bash
curl -u admin:admin http://<device-ip>/frame --output test.jpg
open test.jpg      # macOS
xdg-open test.jpg  # Linux
```

### Open the MJPEG stream

```
http://<device-ip>:81/stream
```

Open this URL in a browser or VLC. You should see live video.

---

## Step 7 — Configure WiFi permanently (NVS)

Credentials entered via the AP captive portal are stored in encrypted NVS flash.
They survive OTA updates and reboots.

To update credentials later: Settings → Credentials in the web dashboard,
or via API:

```bash
curl -u admin:admin -X POST http://<device-ip>/credentials \
  -H "Content-Type: application/json" \
  -d '{"wifi_ssid":"MyNetwork","wifi_password":"MyPass","http_user":"myuser","http_pass":"mypass"}'
```

---

## OTA updates (no USB needed after first flash)

Once the camera is on WiFi, all future updates go over-the-air.

**Option A — PlatformIO OTA target:**

Edit `firmware/platformio.ini`, set your camera IP in the `[env:ota]` section:

```ini
[env:ota]
upload_port = 192.168.x.x   ; ← your camera IP
```

Then:

```bash
pio run -e ota -t upload
```

**Option B — web dashboard:**

1. Build the firmware: `pio run`
2. Open `http://<device-ip>/` → Tools → OTA Update
3. Upload `.pio/build/esp32-s3-devkitc-1/firmware.bin`

The device reboots automatically after a successful update.
If the new firmware fails to boot within 10 seconds, it rolls back to the previous version.

---

## Flash pre-built binary (no compiler needed)

Download `firmware.bin`, `bootloader.bin`, and `partitions.bin` from
[GitHub Releases](../../releases/latest), then:

```bash
pip install esptool

esptool.py --chip esp32s3 --port /dev/ttyACM0 --baud 460800 write_flash \
  0x0000  bootloader.bin \
  0x8000  partitions.bin \
  0x10000 firmware.bin
```

> Windows: replace `/dev/ttyACM0` with `COM3`
> macOS: replace with `/dev/cu.usbserial-*` or `/dev/cu.SLAB_USBtoUART`

---

## Troubleshooting

### Build fails: `PSRAM` or `memory type` error

```
error: 'CONFIG_SPIRAM_MODE_OCT' undeclared
```

The arduino-esp32 platform version is wrong. The project pins `espressif32@6.12.0`
with `arduino-esp32 3.0.0`. Run:

```bash
pio pkg update
pio run --target clean
pio run
```

### Build fails: Edge Impulse / FOMO model not found

```
fatal error: ei_classifier_porting.h: No such file or directory
```

The FOMO model library is missing from `firmware/lib/`. Make sure you cloned
the full repo including submodules:

```bash
git clone --recurse-submodules https://github.com/PeterkoCZ91/DFR1154-ai-camera.git
```

If you already cloned without `--recurse-submodules`:

```bash
git submodule update --init --recursive
```

### Flash fails: "Failed to connect to ESP32-S3"

1. Hold the **BOOT** button while the tool prints `Connecting...`, release after
2. Try a lower baud rate: `pio run --target upload -- --baud 460800`
3. Try a different USB cable (data cable, not charge-only)
4. On Windows: install the [CP210x or CH340 driver](https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers) if no COM port appears

### Camera image is black / inverted / all-white after boot

The OV3660 flex cable is loose or inserted incorrectly.
- Power off the board
- Reseat the flex cable — it clicks into a ZIF connector
- Power on and watch serial for `[CAMERA] OV3660 init OK`

If the log shows `OV3660 init FAIL` after three retries, the cable is still not seated.

### "DMA overflow" crash at boot

```
cam_hal: DMA overflow
Guru Meditation Error: Core 0 panic'ed (Cache disabled but cached memory region accessed)
```

This is an arduino-esp32 regression in versions 3.3.0+.
The project pins to 3.0.0 — if you see this, run `pio pkg update` and rebuild.

### Serial monitor shows garbage characters

Wrong baud rate. The firmware uses **115200 baud**. In PlatformIO this is set
automatically via `monitor_speed = 115200` in `platformio.ini`.

If using another tool (Arduino IDE, minicom): set 115200 8N1.

### AP mode: captive portal doesn't open automatically

On some Android phones the captive portal auto-open is blocked.
Open `http://192.168.4.1` manually in your browser.

On iOS: connect to `ESP32-Camera-Setup`, wait 5 seconds for the notification
"Sign in to network", tap it. If it doesn't appear, open `http://192.168.4.1` manually.

---

## What's next

- **Enable person detection** — Dashboard → Person AI: ON
- **Connect to Home Assistant** — Settings → MQTT, enter your broker IP
- **Set up Telegram alerts** — Settings → Telegram, paste bot token + chat ID
- **Add A12 server-side YOLO** — see [`a12_system/DOCKER.md`](../a12_system/DOCKER.md)
- **Full API reference** — [`README.md`](../README.md#api-reference)
