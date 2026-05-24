#include "motion_detection.h"
#include "img_converters.h"
#include <esp_heap_caps.h>
#include "esp_camera.h"

MotionDetector motionDetector;

// ── Helpers ──────────────────────────────────────────────────────────────────

// BT.601 RGB565 → luminance (same weights as M5Stack version)
static inline uint8_t rgb565ToGray(uint16_t pixel) {
    uint8_t r = (pixel >> 11) & 0x1F;
    uint8_t g = (pixel >> 5)  & 0x3F;
    uint8_t b =  pixel        & 0x1F;
    return (uint8_t)((77 * (r << 3) + 150 * (g << 2) + 29 * (b << 3)) >> 8);
}

// Box-filter (area-average) downsample: RGB565 srcW×srcH → grayscale dstW×dstH.
// Every source pixel that maps into a destination cell contributes — eliminates
// the aliasing and noise amplification that nearest-neighbour caused on sensor-
// noise dominated scenes (the key improvement over the old approach).
static void rgb565BoxFilterToGray(const uint8_t* rgb565, int srcW, int srcH,
                                   uint8_t* gray, int dstW, int dstH) {
    const uint16_t* px = (const uint16_t*)rgb565;
    for (int y = 0; y < dstH; y++) {
        int sy0 = (y * srcH) / dstH;
        int sy1 = ((y + 1) * srcH) / dstH;
        if (sy1 <= sy0) sy1 = sy0 + 1;
        for (int x = 0; x < dstW; x++) {
            int sx0 = (x * srcW) / dstW;
            int sx1 = ((x + 1) * srcW) / dstW;
            if (sx1 <= sx0) sx1 = sx0 + 1;
            uint32_t sum = 0;
            int cnt = 0;
            for (int iy = sy0; iy < sy1; iy++) {
                const uint16_t* row = px + iy * srcW;
                for (int ix = sx0; ix < sx1; ix++) {
                    sum += rgb565ToGray(row[ix]);
                    cnt++;
                }
            }
            gray[y * dstW + x] = (uint8_t)(sum / cnt);
        }
    }
}

// 4×4 block averaging: MOTION_DECODE_W×MOTION_DECODE_H → MOTION_GRID_W×MOTION_GRID_H
static void computeBlockGrid(const uint8_t* gray, uint8_t* blocks) {
    for (int by = 0; by < MOTION_GRID_H; by++) {
        for (int bx = 0; bx < MOTION_GRID_W; bx++) {
            uint32_t sum = 0;
            for (int dy = 0; dy < MOTION_BLOCK_SIZE; dy++)
                for (int dx = 0; dx < MOTION_BLOCK_SIZE; dx++)
                    sum += gray[(by * MOTION_BLOCK_SIZE + dy) * MOTION_DECODE_W +
                                 bx * MOTION_BLOCK_SIZE + dx];
            blocks[by * MOTION_GRID_W + bx] = (uint8_t)(sum / (MOTION_BLOCK_SIZE * MOTION_BLOCK_SIZE));
        }
    }
}

// 8-connectivity spatial filter — removes isolated blocks (single-pixel noise artifacts)
static int spatialFilter8(const uint8_t* mask, uint8_t* filtered) {
    int clustered = 0;
    for (int y = 0; y < MOTION_GRID_H; y++) {
        for (int x = 0; x < MOTION_GRID_W; x++) {
            int idx = y * MOTION_GRID_W + x;
            if (!mask[idx]) { filtered[idx] = 0; continue; }
            int neighbors = 0;
            for (int dy = -1; dy <= 1; dy++) {
                for (int dx = -1; dx <= 1; dx++) {
                    if (dx == 0 && dy == 0) continue;
                    int nx = x + dx, ny = y + dy;
                    if (nx >= 0 && nx < MOTION_GRID_W && ny >= 0 && ny < MOTION_GRID_H)
                        if (mask[ny * MOTION_GRID_W + nx]) neighbors++;
                }
            }
            filtered[idx] = (neighbors >= 1) ? 1 : 0;
            if (filtered[idx]) clustered++;
        }
    }
    return clustered;
}

// ── Class ─────────────────────────────────────────────────────────────────────

MotionDetector::MotionDetector()
    : rgb565_buf(nullptr), gray_buf(nullptr),
      motion_detected(false), motion_score(0), motion_pct(0.0f),
      confirm_counter(0), frame_counter(0),
      avg_brightness(128), prev_brightness(128),
      last_check_time(0), last_agc_gain(0) {
    memset(bgModel,      0, sizeof(bgModel));
    memset(blockGrid,    0, sizeof(blockGrid));
    memset(diffMask,     0, sizeof(diffMask));
    memset(prevDiffMask, 0, sizeof(prevDiffMask));
    memset(block_motion, 0, sizeof(block_motion));
    clearMask();
}

MotionDetector::~MotionDetector() {
    if (rgb565_buf) { free(rgb565_buf); rgb565_buf = nullptr; }
    if (gray_buf)   { free(gray_buf);   gray_buf   = nullptr; }
}

bool MotionDetector::init() {
    if (rgb565_buf && gray_buf) return true;  // already initialised

    // Two small PSRAM buffers (was ~768 KB total, now ~100 KB)
    size_t rgb_bytes  = MOTION_DECODE_MAX_W * MOTION_DECODE_MAX_H * 2;
    size_t gray_bytes = MOTION_DECODE_W * MOTION_DECODE_H;

    if (psramFound()) {
        rgb565_buf = (uint8_t*)heap_caps_malloc(rgb_bytes,  MALLOC_CAP_SPIRAM);
        gray_buf   = (uint8_t*)heap_caps_malloc(gray_bytes, MALLOC_CAP_SPIRAM);
    } else {
        rgb565_buf = (uint8_t*)malloc(rgb_bytes);
        gray_buf   = (uint8_t*)malloc(gray_bytes);
    }

    if (!rgb565_buf || !gray_buf) {
        Serial.println("[Motion] PSRAM alloc failed!");
        if (rgb565_buf) { free(rgb565_buf); rgb565_buf = nullptr; }
        if (gray_buf)   { free(gray_buf);   gray_buf   = nullptr; }
        return false;
    }

    for (int i = 0; i < MOTION_GRID_W * MOTION_GRID_H; i++) bgModel[i] = 128.0f;
    frame_counter = 0;
    clearMask();

    Serial.printf("[Motion] Init OK — decode %dx%d, grid %dx%d, PSRAM %u KB\n",
                  MOTION_DECODE_W, MOTION_DECODE_H, MOTION_GRID_W, MOTION_GRID_H,
                  (unsigned)(rgb_bytes + gray_bytes) / 1024);
    return true;
}

// Approximate JPG_SCALE_8X output dimensions from original frame size.
// tjpgd rounds down to MCU-aligned multiples of 8; for the resolutions we
// use the result is exactly frame_w/8 × frame_h/8.
void MotionDetector::decodedDims(int frame_w, int frame_h, int& decW, int& decH) const {
    decW = frame_w / 8;
    decH = frame_h / 8;
    if (decW < 1) decW = 1;
    if (decH < 1) decH = 1;
    if (decW > MOTION_DECODE_MAX_W) decW = MOTION_DECODE_MAX_W;
    if (decH > MOTION_DECODE_MAX_H) decH = MOTION_DECODE_MAX_H;
}

bool MotionDetector::decodeAndSubsample(const uint8_t* jpeg, size_t jpeg_len,
                                         int frame_w, int frame_h) {
    if (!rgb565_buf || !gray_buf || !jpeg || jpeg_len == 0) return false;

    // Decode JPEG at 1/8 scale via tjpgd
    if (!jpg2rgb565(jpeg, jpeg_len, rgb565_buf, JPG_SCALE_8X)) return false;

    int decW, decH;
    decodedDims(frame_w, frame_h, decW, decH);

    // Box-filter subsample decoded RGB565 → 80×60 grayscale
    rgb565BoxFilterToGray(rgb565_buf, decW, decH,
                          gray_buf, MOTION_DECODE_W, MOTION_DECODE_H);

    // 4×4 block averaging → 20×15 block grid
    computeBlockGrid(gray_buf, blockGrid);
    return true;
}

bool MotionDetector::compareBlocks() {
    // Compute average brightness and sensor AGC gain
    uint32_t brightnessSum = 0;
    for (int i = 0; i < MOTION_GRID_W * MOTION_GRID_H; i++) brightnessSum += blockGrid[i];
    avg_brightness = (uint8_t)(brightnessSum / (MOTION_GRID_W * MOTION_GRID_H));

    sensor_t* s = esp_camera_sensor_get();
    int gain = (s != nullptr) ? s->status.agc_gain : 0;
    last_agc_gain = gain;

    bool isNight = (avg_brightness < MOTION_NIGHT_BRIGHTNESS);
    float alpha  = isNight ? MOTION_EMA_ALPHA_NIGHT : MOTION_EMA_ALPHA_DAY;

    // Training period: build background model, no triggers
    frame_counter++;
    if (frame_counter <= MOTION_EMA_TRAINING) {
        float trainAlpha = 0.5f;
        for (int i = 0; i < MOTION_GRID_W * MOTION_GRID_H; i++)
            bgModel[i] = bgModel[i] * trainAlpha + (float)blockGrid[i] * (1.0f - trainAlpha);
        if (frame_counter == MOTION_EMA_TRAINING)
            Serial.printf("[Motion] Background trained (%d frames, brightness=%d)\n",
                          frame_counter, avg_brightness);
        motion_detected = false;
        return false;
    }

    // AGC-adaptive threshold + hysteresis.
    // While motion is active, lower to 0.7× so a continuous event doesn't
    // flicker off between frames as the subject moves slightly.
    float threshold = (float)MOTION_PIXEL_THRESHOLD + (float)gain * 1.0f;
    if (motion_detected) threshold *= 0.70f;

    // Sudden brightness jump → global light event, reset background
    int brightnessDelta = abs((int)avg_brightness - (int)prev_brightness);
    if (prev_brightness > 0 && brightnessDelta > 40) {
        for (int i = 0; i < MOTION_GRID_W * MOTION_GRID_H; i++)
            bgModel[i] = (float)blockGrid[i];
        confirm_counter = 0;
        motion_detected = false;
        Serial.printf("[Motion] Brightness jump %d→%d — bg reset\n", prev_brightness, avg_brightness);
        prev_brightness = avg_brightness;
        return false;
    }
    prev_brightness = avg_brightness;

    // Save previous diff mask for temporal filter
    memcpy(prevDiffMask, diffMask, sizeof(diffMask));

    int activeBlocks = 0;
    memset(diffMask, 0, sizeof(diffMask));

    for (int i = 0; i < MOTION_GRID_W * MOTION_GRID_H; i++) {
        float diff = fabsf((float)blockGrid[i] - bgModel[i]);
        // Relative threshold: bright blocks (window, sky) need a proportionally
        // larger change to trigger — prevents backlit-scene false positives.
        float relDiff = diff / (bgModel[i] + 1.0f);
        if (diff > threshold && relDiff > MOTION_REL_THRESHOLD) {
            diffMask[i] = 1;
            activeBlocks++;
            // Slowly drift motion blocks toward current frame so gradual illumination
            // changes (clouds, sunset) don't accumulate into permanent false triggers.
            bgModel[i] = bgModel[i] * MOTION_EMA_ALPHA_MOTION + (float)blockGrid[i] * (1.0f - MOTION_EMA_ALPHA_MOTION);
        } else {
            bgModel[i] = bgModel[i] * alpha + (float)blockGrid[i] * (1.0f - alpha);
        }
    }

    // Upper bound: > MOTION_UPPER_PCT% changed → global illumination shift, not motion
    float activePct = (float)activeBlocks * 100.0f / (MOTION_GRID_W * MOTION_GRID_H);
    if (activePct > MOTION_UPPER_PCT) {
        // Reset background to current to recover quickly after the light change
        for (int i = 0; i < MOTION_GRID_W * MOTION_GRID_H; i++)
            bgModel[i] = bgModel[i] * 0.5f + (float)blockGrid[i] * 0.5f;
        confirm_counter = 0;
        motion_detected = false;
        return false;
    }

    // Spatial filter (8-connectivity) — removes isolated noise blocks
    uint8_t filtered[MOTION_GRID_W * MOTION_GRID_H];
    int clusteredBlocks = spatialFilter8(diffMask, filtered);
    memcpy(diffMask, filtered, sizeof(diffMask));
    memcpy(block_motion, diffMask, sizeof(block_motion));  // expose to callers

    float clusteredPct = (float)clusteredBlocks * 100.0f / (MOTION_GRID_W * MOTION_GRID_H);

    // Night mode: stricter cluster requirement AND 2.5× trigger percentage
    if (isNight) {
        int strictClustered = 0;
        for (int y = 0; y < MOTION_GRID_H; y++) {
            for (int x = 0; x < MOTION_GRID_W; x++) {
                int idx = y * MOTION_GRID_W + x;
                if (!diffMask[idx]) continue;
                int neighbors = 0;
                for (int dy = -1; dy <= 1; dy++) for (int dx = -1; dx <= 1; dx++) {
                    if (dx == 0 && dy == 0) continue;
                    int nx = x + dx, ny = y + dy;
                    if (nx >= 0 && nx < MOTION_GRID_W && ny >= 0 && ny < MOTION_GRID_H)
                        if (diffMask[ny * MOTION_GRID_W + nx]) neighbors++;
                }
                if (neighbors >= 2) strictClustered++;
            }
        }
        clusteredBlocks = strictClustered;
        clusteredPct = (float)strictClustered * 100.0f / (MOTION_GRID_W * MOTION_GRID_H);
        if (clusteredPct < MOTION_TRIGGER_PCT * 2.5f) {
            confirm_counter = 0;
            motion_detected = false;
            motion_score = 0; motion_pct = clusteredPct;
            return false;
        }
    }

    // ROI mask: recalculate active blocks excluding masked-out blocks
    int roiActive = 0;
    for (int i = 0; i < MOTION_GRID_W * MOTION_GRID_H; i++)
        if (block_mask[i]) roiActive++;
    if (roiActive == 0) roiActive = MOTION_GRID_W * MOTION_GRID_H;

    int roiMotion = 0;
    for (int i = 0; i < MOTION_GRID_W * MOTION_GRID_H; i++)
        if (diffMask[i] && block_mask[i]) roiMotion++;

    float roiPct = (float)roiMotion * 100.0f / roiActive;

    motion_score = roiMotion;
    motion_pct   = roiPct;

    // Temporal filter: require MOTION_CONFIRM_FRAMES consecutive frames
    bool frameHasMotion = (roiPct >= MOTION_TRIGGER_PCT);
    if (frameHasMotion) confirm_counter++;
    else confirm_counter = 0;

    bool confirmed = (confirm_counter >= MOTION_CONFIRM_FRAMES);

    if (confirmed != motion_detected) {
        motion_detected = confirmed;
        if (confirmed)
            Serial.printf("[Motion] DETECTED: blocks=%d/roi=%d (%.1f%%), thresh=%.1f, gain=%d, bright=%d\n",
                          roiMotion, roiActive, roiPct, threshold, gain, avg_brightness);
        else
            Serial.printf("[Motion] Cleared: %.1f%%\n", roiPct);
    }

    return motion_detected;
}

// ── Public API ────────────────────────────────────────────────────────────────

bool MotionDetector::detect(camera_fb_t* fb) {
    if (!rgb565_buf || !gray_buf || !fb) return false;
    if (millis() - last_check_time < MOTION_CHECK_INTERVAL_MS) return motion_detected;
    last_check_time = millis();
    if (fb->format != PIXFORMAT_JPEG) return false;
    if (!decodeAndSubsample(fb->buf, fb->len, fb->width, fb->height)) return false;
    return compareBlocks();
}

bool MotionDetector::detectFromJpeg(const uint8_t* jpeg, size_t jpeg_len,
                                     int frame_w, int frame_h) {
    if (!rgb565_buf || !gray_buf || !jpeg || jpeg_len == 0) return false;
    if (millis() - last_check_time < MOTION_CHECK_INTERVAL_MS) return motion_detected;
    last_check_time = millis();
    if (!decodeAndSubsample(jpeg, jpeg_len, frame_w, frame_h)) return false;
    return compareBlocks();
}

bool    MotionDetector::isMotionDetected()  const { return motion_detected; }
int     MotionDetector::getMotionScore()    const { return motion_score; }
float   MotionDetector::getMotionPercent()  const { return motion_pct; }
uint8_t MotionDetector::getAvgBrightness()  const { return avg_brightness; }

int MotionDetector::getEffectivePixelThreshold() const {
    float threshold = (float)MOTION_PIXEL_THRESHOLD + (float)last_agc_gain * 1.0f;
    if (motion_detected) threshold *= 0.70f;
    return (int)threshold;
}

bool MotionDetector::getBlockMotion(int bx, int by) const {
    if (bx >= 0 && bx < MOTION_GRID_W && by >= 0 && by < MOTION_GRID_H)
        return block_motion[by * MOTION_GRID_W + bx];
    return false;
}

void MotionDetector::setBlockMask(int bx, int by, bool active) {
    if (bx >= 0 && bx < MOTION_GRID_W && by >= 0 && by < MOTION_GRID_H)
        block_mask[by * MOTION_GRID_W + bx] = active;
}

bool MotionDetector::getBlockMask(int bx, int by) const {
    if (bx >= 0 && bx < MOTION_GRID_W && by >= 0 && by < MOTION_GRID_H)
        return block_mask[by * MOTION_GRID_W + bx];
    return true;
}

void MotionDetector::clearMask() {
    memset(block_mask, 1, sizeof(block_mask));
}
