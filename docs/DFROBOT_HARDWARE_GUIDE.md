# DFRobot FireBeetle 2 ESP32-S3 (AI Camera) — Hardware Guide

Hardware capabilities and pin mapping for the DFRobot ESP32-S3 AI Camera (DFR1154), derived from analysis of the official DFR1154_Examples.

---

## 1. Audio System

### Microphone (Built-in)

| Property | Value |
|----------|-------|
| Type | PDM (Pulse Density Modulation) |
| Interface | I2S RX |
| CLK pin | GPIO **38** |
| DATA pin | GPIO **39** |

> **Note:** Requires either Arduino Core 3.x (new I2S driver) or specific configuration on Core 2.x for correct operation on ESP32-S3.

### Speaker / Amplifier (Built-in)

| Property | Value |
|----------|-------|
| Chip | MAX98357 (I2S Class-D amplifier) |
| Interface | I2S TX |
| BCLK pin | GPIO **46** |
| LRC (WS) pin | GPIO **45** |
| DIN (Data In) pin | GPIO **42** |

Use cases: alarm tones, TTS (Text-to-Speech), two-way intercom audio.

---

## 2. Sensors and Peripherals

### Ambient Light Sensor

| Property | Value |
|----------|-------|
| Chip | LTR-308 |
| Interface | I2C |
| I2C pins | SDA = GPIO **8**, SCL = GPIO **9** (shared with camera SCCB) |
| Library | `DFRobot_LTR308` |

Used for automatic DAY/DUSK/NIGHT camera profile switching based on real ambient illumination. **The LTR-308 I2C bus shares GPIO 8/9 with the OV3660 SCCB bus** — reads must be scheduled from `captureTask` after `fb_return()` to avoid contention.

> **Enclosures need a second cutout, not just one for the lens.** The LTR-308 sits right next to the lens on the module and needs its own line of sight to ambient light. A generic waterproof junction box drilled with a single hole for the lens seals the LTR-308 behind opaque plastic — it then reads near-zero lux permanently, *regardless of actual room lighting*, which locks `updateCameraProfile()` (`main.cpp`) into `NIGHT` (AGC forced off, heavy denoise) even in broad daylight and produces a flat, washed-out, low-detail image. This is easy to misdiagnose as a hung sensor or a hardware fault (both were ruled out — soft reboot, full power-cycle, and IR-off all failed to change the image — before the sealed enclosure was found) because the symptom looks identical to a real AEC/sensor wedge. Check `GET /status` → `camera_profile` and `GET /ir-control` → `ambient_light_lux` first: a lux reading near 0 in a lit room, on a board that otherwise streams a sharp image when the profile is forced to `DUSK`, means the enclosure is blocking the sensor, not a fault in the camera itself. Fix by drilling a second small opening over the LTR-308, or, if that isn't practical, set `time_based: true` (with `night_start_hour`/`night_end_hour`) via `POST /ir-control` — as of firmware 3.12.49+, this also drives `updateCameraProfile()`'s DUSK/NIGHT choice by clock instead of by lux, bypassing a sensor that can't see anything.

### IR LED (Night Vision Illuminator)

- Controlled via `IR_LEDS_PIN` (GPIO **2** or **48** depending on board revision)
- Auto-control: activated when lux < 5 (NIGHT profile), deactivated when lux > 10 (hysteresis)
- Manual override via `/ir-control` API or dashboard toggle

> **A manual `on`/`off` disables the night schedule permanently.** `updateIRAutoMode()` returns immediately when `auto_mode` is false, so `time_based` / `night_start_hour` / `night_end_hour` are never evaluated and the LED stays pinned to `manual_state` forever. `POST /ir-control` sets `auto_mode = false` by itself for `state: "on"` and `state: "off"` — only `state: "auto"` puts it back. One toggle from a dashboard or a Home Assistant switch therefore kills the night automation until someone notices. Check `GET /ir-status`: `auto_mode: false` with a stale `last_update_ms` is the signature.

> **Turning both the IR LED and the NIGHT profile off, without a firmware change:** set `night_start_hour` equal to `night_end_hour` (e.g. both `0`). Both `updateIRAutoMode()` and `updateCameraProfile()` compute night as `(start > end) ? (h >= start || h < end) : (h >= start && h < end)`, so equal hours can never be night — the LED stays off and the profile stays on `DUSK` (full-auto AEC/AGC). Useful when the illuminator is blocked by the enclosure, or when the NIGHT profile causes more trouble than it solves. Persisted to LittleFS `/ir_config.json`, so it survives a reboot.

### Status LED

| Property | Value |
|----------|-------|
| Pin | GPIO **3** |
| Use | Boot / WiFi connected / Recording active status indication |

### Buttons

| Button | GPIO |
|--------|------|
| Boot | GPIO **0** |
| Reset | Hardware reset (dedicated pin) |

---

## 3. Camera

| Property | Value |
|----------|-------|
| Sensor | OV3660 (3 MP) |
| Interface | DVP (Parallel) |
| XCLK | GPIO 10 |
| PCLK | GPIO 12 |
| VSYNC | GPIO 13 |
| HREF | GPIO 14 |
| D0–D7 | GPIO 35, 36, 37, 34, 33, 48, 47, 21 |
| SIOD (SDA) | GPIO **8** (shared with LTR-308 I2C) |
| SIOC (SCL) | GPIO **9** (shared with LTR-308 I2C) |
| PWDN | -1 (not wired) |
| RESET | -1 (not wired) |

> **SCCB / I2C conflict:** SIOD/SIOC share the same GPIO as the LTR-308. Call `Wire.begin(8, 9)` **before** `esp_camera_init()`, then configure the camera with `pin_sccb_sda = -1` and `sccb_i2c_port = 0` so the camera driver reuses Wire's already-installed I2C driver instead of installing its own. Failure to do this causes all runtime register writes (vflip, hmirror, brightness, etc.) to silently fail.

> **OV3660 cannot upscale:** Always initialise at the highest resolution you may need (UXGA 1600×1200). Runtime frame-size changes only downscale; attempting to upscale returns corrupted frames.

---


### Uniform-grey frames: a wedged exposure loop, not darkness

The OV3660 auto-exposure loop can wedge into a state where the sensor streams perfectly decodable JPEGs at full frame rate that are a **uniform field with no detail at all**. Because frames keep arriving, transport-level watchdogs (stream freeze, no-frame timeouts) never trip, and the camera can stay blind indefinitely — observed 2026-09-11/12 for 12 hours straight.

What does **not** clear it:

- re-applying the day/night profile (a `NIGHT` -> `DUSK` switch changed `brightness` 5.0 -> 64.1 and restored no detail)
- forcing the IR LED off
- a soft `POST /reboot` — one such wedge survived five LAN reboots

What does clear it is writing the exposure registers explicitly:

```bash
curl -su admin:admin -X POST -H 'Content-Type: application/json' \
     -d '{"aec":0,"agc":0,"agc_gain":0,"aec_value":300}' http://<camera>/settings
# then hand control back:
curl -su admin:admin -X POST -H 'Content-Type: application/json' \
     -d '{"aec":1,"agc":1}' http://<camera>/settings
```

This doubles as the diagnostic. Detail appearing on the first write alone proves the sensor, the optics and the light level are all fine and the exposure loop was the problem. Note that `/settings` is behind basic auth — an unauthenticated POST returns 401.

**Do not read a uniform frame as "it is dark".** A mid-grey `brightness` around 64 with `std` near 0 is a wedge; genuine darkness on this board reads `brightness` around 5. Image statistics alone cannot tell a featureless scene from a fault, which is why the rewrite above is the right response and a reboot is not: it costs two HTTP writes, no downtime, and is a no-op on a scene that really is dark.

## 4. Other Features

### SD Card

- Integrated slot (SDMMC or SPI mode)
- FAT32 only — exFAT not supported by the ESP32 Arduino SD library
- Minimum Class 10 / U1 for AVI recording at 10 FPS UXGA (~3 MB/s sustained)

### USB Webcam Mode

The board can operate as a USB UVC device (see official example `5.4 USBWebCamera`).

---

## 5. Running Two Boards on One Network

`config.device_name` (default `ESP32-Camera`) is used as **both** the mDNS hostname and the MQTT topic prefix (`esp32cam/<device_name>`). A second board flashed with the stock firmware therefore does not simply appear alongside the first — it collides with it:

- `<device_name>.local` starts resolving to whichever board answers, so a companion that addresses the camera by mDNS can silently talk to the wrong device.
- mDNS conflict resolution renames a responder (you see `ESP32-Camera-2` appear in `avahi-browse`), and the *original* board can be the one that loses the name — after which the expected hostname resolves to nothing until it is rebooted.
- Both boards publish to the same MQTT topics, so one board's motion events are consumed as the other's.

Give the second board its own `device_name` before putting it on the same LAN. Note that `/config.json` on LittleFS overrides the compiled default, so changing the default in `getDefaultConfig()` is not enough on a board that already has a saved config — erase the filesystem partition as well:

```bash
# partition offsets come from firmware/partitions.csv (spiffs at 0xc10000)
esptool --port /dev/ttyACM0 --chip esp32s3 erase-region 0xc10000 0x3F0000
```

WiFi, Telegram and HTTP credentials live in **NVS**, not in `/config.json`, so that erase does not cost the board its network connection.

## 6. Development Notes (A12 System)

1. **Microphone:** Use GPIO 38/39 with Arduino Core 3.0.0 (pinned in `platformio.ini`). Core 3.3.0+ has a DMA overflow regression with FOMO inference — stay on 3.0.0.
2. **LTR-308 reads:** Always read from `captureTask` after `fb_return()`. Direct reads from other tasks cause I2C bus contention with SCCB and return stale/garbage values.
3. **Speaker (intercom):** MAX98357 on GPIO 42/45/46 is ready to use — two-way audio is on the roadmap.
4. **IR LED auto-control:** LTR-308 100 ms sample rate (configured in `ir_handler.cpp`) gives 5× faster reaction to sudden light changes (headlights, room lights on/off) compared to the original 500 ms setting.

## Reading the camera's reboot log: which reason is the cause

`/events` interleaves two different reboot reasons and they mean opposite
things. Getting them the wrong way round costs an evening.

| `boot` detail | What it is |
|---|---|
| `reason=PANIC(4)` | **The firmware crashed.** This is a cause. |
| `reason=SW(3)` preceded by `reboot_cmd` | A12's transport watchdog rebooting the camera after the stream died. This is a **consequence** — cleanup, not a fault in itself. |

A camera that crashes produces both, alternating, because every panic drops the
stream and A12 then reboots what it thinks is a hung camera. Count the
`PANIC(4)` entries, not the restarts.

The same trap exists one level down in the stream statistics: a rising
`send_fail_count` with `last_errno=104` is the camera observing A12's own
teardown, not the camera dropping us.

## Putting a bench board into the production role

A board flashed for bench use does not become a production camera by pointing
A12 at it. Measured on 2026-09-13, the day after such a swap: **45 restarts,
four an hour**, with `PANIC(4)` between them, and stall spikes of 105-141 an
hour. The board still had the bench configuration.

In the PIR-first architecture the camera is **only a frame source** — the
detection and the notifications belong to A12. So before the swap counts as
done:

```
POST /settings {"motion_detection_enabled": false,
                "person_detection_enabled": false,
                "person_telegram_photo":    false,
                "motion_telegram_photo":    false,
                "motion_telegram_video":    false}
```

Turning those off on 2026-09-13 18:19 ended the crashes: zero panics and 92
minutes of unbroken uptime in the hour that followed, against four restarts in
the hour before. One hour is a strong signal, not proof — the panics came every
200-700 s, so a day of clean numbers is the confirmation.

**Side effect worth knowing:** disabling firmware motion silences
`esp32cam/<device>/motion`, which A12 subscribes to as a *secondary* trigger.
The primary trigger is the PIR (`MOTION_THRESHOLD=0`), so detection continues,
but with one layer fewer.
