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

### IR LED (Night Vision Illuminator)

- Controlled via `IR_LEDS_PIN` (GPIO **2** or **48** depending on board revision)
- Auto-control: activated when lux < 5 (NIGHT profile), deactivated when lux > 10 (hysteresis)
- Manual override via `/ir-control` API or dashboard toggle

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

## 4. Other Features

### SD Card

- Integrated slot (SDMMC or SPI mode)
- FAT32 only — exFAT not supported by the ESP32 Arduino SD library
- Minimum Class 10 / U1 for AVI recording at 10 FPS UXGA (~3 MB/s sustained)

### USB Webcam Mode

The board can operate as a USB UVC device (see official example `5.4 USBWebCamera`).

---

## 5. Development Notes (A12 System)

1. **Microphone:** Use GPIO 38/39 with Arduino Core 3.0.0 (pinned in `platformio.ini`). Core 3.3.0+ has a DMA overflow regression with FOMO inference — stay on 3.0.0.
2. **LTR-308 reads:** Always read from `captureTask` after `fb_return()`. Direct reads from other tasks cause I2C bus contention with SCCB and return stale/garbage values.
3. **Speaker (intercom):** MAX98357 on GPIO 42/45/46 is ready to use — two-way audio is on the roadmap.
4. **IR LED auto-control:** LTR-308 100 ms sample rate (configured in `ir_handler.cpp`) gives 5× faster reaction to sudden light changes (headlights, room lights on/off) compared to the original 500 ms setting.
