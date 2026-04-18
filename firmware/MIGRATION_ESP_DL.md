# Migration Guide: FOMO → ESP-DL YOLOv11n

**Status:** Attempted 2026-04-10, blocked by PlatformIO tooling (see post-mortem below)
**Realistic effort:** 5-8 days (not 2-3 as originally estimated)
**Risk:** High — unknown territory, no public PlatformIO+arduino+ESP-DL integration exists

This guide documents the migration from Edge Impulse FOMO to Espressif's ESP-DL YOLOv11n pedestrian detection. It was written before a concrete attempt revealed several wrong assumptions; the post-mortem at the bottom lists what actually works.

---

## Post-mortem — attempted spike 2026-04-10

One-day spike in a sibling `Verze: 4.0.0 (ESP-DL YOLOv11n)/` working copy.

**Phase 0 + 1 (success):**
- Default `platformio/platform-espressif32` has no Linux toolchain for arduino-esp32 3.3.5 — switched to the **pioarduino fork release `55.03.35`** (= arduino-esp32 3.3.5 + ESP-IDF 5.5.1).
- Pioarduino ships only the libs, not the framework sources — also needed `platform_packages = framework-arduinoespressif32 @ https://github.com/espressif/arduino-esp32.git#3.3.5` (otherwise `FRAMEWORK_DIR=None`).
- v3.11.0 codebase compiled cleanly under 3.3.5.
- Cost: RAM 32.3% → 36.7% (+14 KB), Flash 25.2% → 25.7% (+27 KB), **IRAM 100% (16384/16384, zero headroom)** — no budget left for ESP-DL's IRAM-resident helpers.

**Phase 2 (hard blocker):**
Pioarduino exposes a `custom_component_add` directive that wraps ESP-IDF Component Manager:

```ini
custom_component_add =
    espressif/esp-dl@^3.3.0
    espressif/pedestrian_detect@^0.3.0
```

A code audit of `~/.platformio/platforms/espressif32/builder/frameworks/` proves this is a **no-op in pure `framework = arduino`**:

| File:line | Behaviour |
|---|---|
| `espidf.py:227-228` | Detects option, sets `flag_custom_component_add = True` |
| `espidf.py:789-790` | Calls `handle_component_settings(add_components=flag, ...)` |
| `espidf.py:2688` | Code path explicitly gated: `if "espidf" in $PIOFRAMEWORK and ...` |
| `arduino.py:538` | Declares `flag_custom_component_add = False` |
| `arduino.py:925` | Calls `handle_component_settings()` with no args → both flags False → no-op |
| `arduino.py` | **Never** sets `flag_custom_component_add = True` anywhere |

Symptom: build "succeeds" with byte-identical memory stats to Phase 1, no `[ComponentManager]` log lines, `idf_component.yml` created in `src/` but contains only `dependencies: { idf: '>=5.1' }` (custom entries dropped), no `managed_components/` directory anywhere.

**Why:** ESP-IDF Component Manager only runs in `framework = espidf` (or hybrid `arduino, espidf`). In pure arduino mode the framework libs are precompiled and the component pipeline is disabled.

**Required to unblock:** switch to `framework = arduino, espidf` hybrid mode — which means writing CMakeLists.txt, sdkconfig.defaults, and restructuring the build (5-8 days before any actual ESP-DL integration). GitHub code search returns **zero** results for `PedestrianDetect` in any `platformio.ini`; no one has publicly integrated ESP-DL on PlatformIO + pure Arduino.

**Errata in the original guide (below):**
- Says `esp-dl @ ^2.0.0` — actual latest on registry is **v3.3.0** (GitHub release tag is v3.2.0, registry has v3.3.0).
- Says `pedestrian_detect ^0.3.0` from `espressif/pedestrian_detect` — component exists at registry v0.3.1. ✓
- Says model file `esp_pedestrian_yolo11n_s8_v1.espdl` from `esp-detection/releases` — `esp-detection` repo has **no releases**, it's a Python training repo. The real model ships embedded inside the `pedestrian_detect` component itself.
- Refactor target API in the guide is wrong. Actual API: `dl::image::sw_decode_jpeg(jpeg_img, RGB888)` → `PedestrianDetect *d = new PedestrianDetect(); auto &res = d->run(img);` returns `vector<dl::detect::result_t>` with `.score` and `.box[0..3]` (xyxy).

**Required reading for a retry:**
- `examples/pedestrian_detect/main/app_main.cpp` in `espressif/esp-dl` — real API usage.
- `models/pedestrian_detect/pedestrian_detect.hpp` — class definition.
- Pioarduino docs on hybrid mode (`framework = arduino, espidf`) — required path.

---

## Original guide (kept for reference, assumptions below are wrong in places)

## Why Migrate

| Aspect | FOMO 64×64 (current) | YOLOv11n (target) |
|--------|---------------------|-------------------|
| Output | Centroids only | Full bounding boxes (x, y, w, h) |
| Resolution | 64×64 grayscale | 224×224 or 320×320 RGB |
| Inference | ~150 ms | ~140 ms (similar) |
| Recall | Low (small objects only) | High (multi-scale) |
| Range | ~5 m | ~20 m (65 ft) |
| FPS | ~6 (cascade gated) | >7 standalone |
| Flash | ~280 KB | ~1.5 MB |
| PSRAM | ~384 KB | ~800 KB |

## Prerequisites

### 1. Arduino-ESP32 Core Upgrade (3.0.0 → 3.3.5+)

`platformio.ini` change:
```ini
platform_packages =
    framework-arduinoespressif32 @ https://github.com/espressif/arduino-esp32.git#3.3.5
    framework-arduinoespressif32-libs @ https://github.com/espressif/arduino-esp32/releases/download/3.3.5/esp32-arduino-libs-3.3.5.zip
```

**Known regressions in 3.3.x:**
- Edge Impulse FOMO `cam_hal: DMA overflow` (forum.edgeimpulse.com/t/14723)
- Solution: Remove FOMO entirely (this migration), don't try to keep both

### 2. ESP-DL Component

ESP-DL is distributed as an ESP-IDF managed component. Add to `platformio.ini`:
```ini
build_flags =
    ...
    -DCONFIG_ESP_DL_USE_PSRAM=1
lib_deps =
    espressif/esp-dl @ ^2.0.0
    espressif/pedestrian_detect @ ^0.3.0
```

Or download manually:
```bash
cd lib/
git clone https://github.com/espressif/esp-dl.git
git clone -b master https://github.com/espressif/esp-detection.git
```

### 3. Model File

Download `esp_pedestrian_yolo11n_s8_v1.espdl` (~1.4 MB) from:
https://github.com/espressif/esp-detection/releases

Place in `data/models/` and configure LittleFS upload.

## Refactor Steps

### Step 1: Replace `person_detection.cpp`

```cpp
#include "pedestrian_detect.hpp"  // ESP-DL
#include "tracker.h"

class PersonDetector {
public:
    bool init() {
        detector = new PedestrianDetect();
        return detector->init();  // Loads model from flash/PSRAM
    }

    PersonDetectionResult detectFromJpeg(const uint8_t* jpeg_data, size_t jpeg_len,
                                          int frame_w, int frame_h) {
        // 1. JPEG → RGB888 (ESP-DL expects RGB888, not RGB565)
        //    Use jpg2rgb() from img_converters.h with JPG_SCALE_NONE
        // 2. Pass to detector->run(rgb_buf, frame_w, frame_h)
        // 3. Parse result.bboxes (vector of dl::detect::result_t)
        // 4. Convert to tracker format and feed objectTracker
        // 5. Return result based on confirmed tracks
    }

private:
    PedestrianDetect* detector;
};
```

### Step 2: Update Memory Allocations

- **Increase PSRAM allocation** for RGB888 buffer (3 bytes/pixel vs RGB565 2 bytes/pixel)
- **Reduce ring buffer slot count** from 3 to 2 if PSRAM headroom too low
- **Move tensor arena to PSRAM** explicitly: `heap_caps_malloc(SIZE, MALLOC_CAP_SPIRAM)`

### Step 3: Camera Pipeline Adjustments

ESP-DL pedestrian model expects 224×224 input. Two options:

**Option A: JPEG decode at scale, resize to 224×224**
```cpp
uint8_t* rgb_buf = (uint8_t*)heap_caps_malloc(224 * 224 * 3, MALLOC_CAP_SPIRAM);
jpg2rgb(jpeg_data, jpeg_len, rgb_buf, JPG_SCALE_4X);  // ~400×300 → bilinear → 224×224
bilinear_resize_rgb(...);
```

**Option B: Use ESP-DL's built-in image preprocessing**
```cpp
dl::image::resize_to_uint8(input_img, 224, 224, output_buf);
```

### Step 4: Remove Edge Impulse Dependencies

Delete:
- `lib/ei-person-detection-fomo/`
- `-DEI_CLASSIFIER_TFLITE_ENABLE_ESP_NN=0` from build_flags
- `-DEI_PORTING_ARDUINO=1` from build_flags
- `#include <Person_detection_FOMO_inferencing.h>`

### Step 5: Test Plan

1. **Unit test**: Single JPEG → bounding boxes (compare against PC Python ESP-DL)
2. **Memory check**: `esp_get_free_heap_size()` before/after init
3. **Latency check**: Average inference time over 100 frames
4. **Accuracy check**: Walk in front of camera at 1m, 3m, 5m, 10m, 15m — count true positives
5. **Stability check**: 24h soak test, watch for DMA overflow / heap fragmentation
6. **Integration check**: Verify motion → AI cascade still works, MQTT/Telegram fire correctly

## Rollback Plan

If migration fails:
1. Restore `platformio.ini` (3.0.0 platform_packages)
2. Restore `person_detection.cpp` from git history
3. Keep `tracker.h/cpp` (works with both FOMO and YOLO)
4. Re-enable Edge Impulse `lib/ei-person-detection-fomo/`

## References

- ESP-DL: https://github.com/espressif/esp-dl
- ESP-Detection: https://github.com/espressif/esp-detection
- Pedestrian Detect Component: https://components.espressif.com/components/espressif/pedestrian_detect
- Forum DMA issue: https://forum.edgeimpulse.com/t/esp32-s3-face-detection-edge-impulse-fomo-memory-issue-guru-meditation-error/14723
