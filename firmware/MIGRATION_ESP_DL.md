# Migration Guide: FOMO → ESP-DL YOLOv11n

**Status:** Planned (Tier 2 F)
**Effort:** 2-3 days
**Risk:** High (major refactor + dependency upgrades)

This guide documents the migration from Edge Impulse FOMO to Espressif's ESP-DL YOLOv11n pedestrian detection.

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
