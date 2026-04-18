#include "motion_detection.h"
#include "img_converters.h"
#include <esp_heap_caps.h>

MotionDetector motionDetector;

MotionDetector::MotionDetector() {
    bg_gray = NULL;
    curr_gray = NULL;
    rgb565_decode_buf = NULL;
    gray_alloc_size = 0;
    rgb565_alloc_size = 0;
    motion_detected = false;
    motion_score = 0;
    motion_pct = 0.0f;
    confirm_counter = 0;
    frame_counter = 0;
    avg_brightness = 128;
    prev_brightness = 128;
    last_check_time = 0;
    width = 0;
    height = 0;
    last_agc_gain = 0;
}

MotionDetector::~MotionDetector() {
    if (bg_gray) free(bg_gray);
    if (curr_gray) free(curr_gray);
    if (rgb565_decode_buf) free(rgb565_decode_buf);
}

bool MotionDetector::init() {
    if (bg_gray && curr_gray && rgb565_decode_buf) {
        Serial.println("ℹ️  MotionDetector buffers already allocated (reusing)");
        return true;
    }

    // Max resolution QXGA (2048x1536) scaled by 1/4 = 512x384
    int max_w = 2048 / 4; // 512
    int max_h = 1536 / 4; // 384
    gray_alloc_size = max_w * max_h;          // 196608 bytes (~192KB) per grayscale buffer
    rgb565_alloc_size = max_w * max_h * 2;    // 393216 bytes (~384KB) for RGB565 decode

    // Default for VGA (640x480) at scale 4 = 160x120 (auto-updated on first frame)
    width = 160;
    height = 120;

    if (psramFound()) {
        bg_gray = (uint8_t*)heap_caps_malloc(gray_alloc_size, MALLOC_CAP_SPIRAM);
        curr_gray = (uint8_t*)heap_caps_malloc(gray_alloc_size, MALLOC_CAP_SPIRAM);
        rgb565_decode_buf = (uint8_t*)heap_caps_malloc(rgb565_alloc_size, MALLOC_CAP_SPIRAM);
        Serial.printf("✅ MotionDetector: Allocated %u KB buffers in PSRAM (EMA model)\n",
                      (gray_alloc_size * 2 + rgb565_alloc_size) / 1024);
    } else {
        bg_gray = (uint8_t*)malloc(gray_alloc_size);
        curr_gray = (uint8_t*)malloc(gray_alloc_size);
        rgb565_decode_buf = (uint8_t*)malloc(rgb565_alloc_size);
        Serial.printf("✅ MotionDetector: Allocated %u KB buffers in Heap\n",
                      (gray_alloc_size * 2 + rgb565_alloc_size) / 1024);
    }

    if (!bg_gray || !curr_gray || !rgb565_decode_buf) {
        Serial.println("❌ MotionDetector: Failed to allocate buffers!");
        if (bg_gray) { free(bg_gray); bg_gray = NULL; }
        if (curr_gray) { free(curr_gray); curr_gray = NULL; }
        if (rgb565_decode_buf) { free(rgb565_decode_buf); rgb565_decode_buf = NULL; }
        return false;
    }

    memset(bg_gray, 0, gray_alloc_size);
    memset(curr_gray, 0, gray_alloc_size);
    memset(block_motion, 0, sizeof(block_motion));
    frame_counter = 0;

    // Initialize ROI mask: all blocks active by default
    clearMask();

    Serial.println("✅ Motion detector initialized (buffers allocated)");
    return true;
}

// Decode JPEG → RGB565 (1/8 scale) → grayscale luminance into curr_gray
bool MotionDetector::decodeToGrayscale(const uint8_t* jpeg_data, size_t jpeg_len,
                                        int frame_w, int frame_h) {
    int new_width = frame_w / SCALE_FACTOR;
    int new_height = frame_h / SCALE_FACTOR;

    size_t rgb565_size = new_width * new_height * 2;
    size_t gray_size = new_width * new_height;

    if (rgb565_size > rgb565_alloc_size || gray_size > gray_alloc_size) {
        return false;
    }

    // Resolution change — reset background model
    if (new_width != width || new_height != height) {
        width = new_width;
        height = new_height;
        memset(bg_gray, 0, gray_alloc_size);
        confirm_counter = 0;
        frame_counter = 0;
    }

    // Step 1: JPEG → RGB565 at 1/4 scale
    bool converted = jpg2rgb565(jpeg_data, jpeg_len, rgb565_decode_buf, JPG_SCALE_4X);
    if (!converted) {
        return false;
    }

    // Step 2: RGB565 → grayscale luminance (BT.601)
    uint16_t* rgb = (uint16_t*)rgb565_decode_buf;
    int num_pixels = width * height;
    for (int i = 0; i < num_pixels; i++) {
        uint16_t pixel = rgb[i];
        uint8_t r = (pixel >> 11) & 0x1F;
        uint8_t g = (pixel >> 5) & 0x3F;
        uint8_t b = pixel & 0x1F;
        // BT.601: Y = 0.299*R + 0.587*G + 0.114*B (scaled to 5/6/5 bit channels)
        curr_gray[i] = (uint8_t)(((r << 3) * 77 + (g << 2) * 150 + (b << 3) * 29) >> 8);
    }

    return true;
}

// Compare curr_gray vs EMA background using block averaging + temporal filter + night suppression
bool MotionDetector::compareFrames() {
    int num_pixels = width * height;
    if (num_pixels <= 0) return false;

    frame_counter++;
    int pixel_thresh = getEffectivePixelThreshold();

    // Block-based comparison: average NxN pixel blocks, compare block averages
    // Block averaging acts as natural low-pass filter — single noisy pixel
    // in 4x4=16 pixel block contributes only 1/16th of the difference
    int bw = width / MOTION_BLOCK_SIZE;   // e.g. 80/4=20 blocks wide
    int bh = height / MOTION_BLOCK_SIZE;  // e.g. 60/4=15 blocks high
    if (bw > MOTION_GRID_W) bw = MOTION_GRID_W;
    if (bh > MOTION_GRID_H) bh = MOTION_GRID_H;
    int total_blocks = bw * bh;
    if (total_blocks <= 0) return false;

    uint32_t brightness_sum = 0;
    int changed_blocks = 0;
    int active_blocks = 0; // Blocks not masked by ROI

    // Clear block motion grid
    memset(block_motion, 0, sizeof(block_motion));

    for (int by = 0; by < bh; by++) {
        for (int bx = 0; bx < bw; bx++) {
            uint32_t curr_sum = 0;
            uint32_t bg_sum = 0;
            int base_y = by * MOTION_BLOCK_SIZE;
            int base_x = bx * MOTION_BLOCK_SIZE;

            for (int py = 0; py < MOTION_BLOCK_SIZE; py++) {
                int row = (base_y + py) * width + base_x;
                for (int px = 0; px < MOTION_BLOCK_SIZE; px++) {
                    int idx = row + px;
                    curr_sum += curr_gray[idx];
                    bg_sum += bg_gray[idx];
                }
            }

            brightness_sum += curr_sum;

            // Skip masked blocks (ROI filter)
            if (bx < MOTION_GRID_W && by < MOTION_GRID_H &&
                !block_mask[by * MOTION_GRID_W + bx]) {
                continue;
            }
            active_blocks++;

            int block_pixels = MOTION_BLOCK_SIZE * MOTION_BLOCK_SIZE;
            uint8_t curr_avg = curr_sum / block_pixels;
            uint8_t bg_avg = bg_sum / block_pixels;

            int diff = abs((int)curr_avg - (int)bg_avg);
            if (diff > pixel_thresh) {
                changed_blocks++;
                if (bx < MOTION_GRID_W && by < MOTION_GRID_H) {
                    block_motion[by * MOTION_GRID_W + bx] = true;
                }
            }
        }
    }
    if (active_blocks == 0) active_blocks = 1; // Prevent div by zero

    avg_brightness = (uint8_t)(brightness_sum / num_pixels);

    // Sudden brightness change (sunset, lights on/off): reset background instantly
    // Without this, EMA would slowly drift over many frames causing false motion
    int brightness_delta = abs((int)avg_brightness - (int)prev_brightness);
    if (prev_brightness > 0 && brightness_delta > 80) {
        // Reset background to current frame (skip EMA blend this round)
        memcpy(bg_gray, curr_gray, num_pixels);
        confirm_counter = 0;  // Clear pending motion
        Serial.printf("Motion: brightness jump %d -> %d, background reset\n",
                      prev_brightness, avg_brightness);
        prev_brightness = avg_brightness;
        return false;
    }
    prev_brightness = avg_brightness;

    // Adaptive EMA: slower adaptation at night (less sensor noise polluting background)
    float alpha = (avg_brightness < 60) ? MOTION_EMA_ALPHA_NIGHT : MOTION_EMA_ALPHA_DAY;
    for (int i = 0; i < num_pixels; i++) {
        bg_gray[i] = (uint8_t)(alpha * bg_gray[i] + (1.0f - alpha) * curr_gray[i]);
    }

    // Spatial clustering: remove isolated motion blocks (salt-and-pepper noise)
    // A block counts only if it has at least one adjacent motion neighbor (4-connected)
    int clustered_blocks = 0;
    for (int by2 = 0; by2 < bh; by2++) {
        for (int bx2 = 0; bx2 < bw; bx2++) {
            if (bx2 >= MOTION_GRID_W || by2 >= MOTION_GRID_H) continue;
            if (!block_motion[by2 * MOTION_GRID_W + bx2]) continue;
            // Check 4-connected neighbors
            bool has_neighbor = false;
            if (bx2 > 0 && block_motion[by2 * MOTION_GRID_W + (bx2-1)]) has_neighbor = true;
            if (bx2 < bw-1 && bx2+1 < MOTION_GRID_W && block_motion[by2 * MOTION_GRID_W + (bx2+1)]) has_neighbor = true;
            if (by2 > 0 && block_motion[(by2-1) * MOTION_GRID_W + bx2]) has_neighbor = true;
            if (by2 < bh-1 && by2+1 < MOTION_GRID_H && block_motion[(by2+1) * MOTION_GRID_W + bx2]) has_neighbor = true;
            if (has_neighbor) {
                clustered_blocks++;
            } else {
                // Remove isolated block from motion grid
                block_motion[by2 * MOTION_GRID_W + bx2] = false;
            }
        }
    }

    motion_score = clustered_blocks;
    motion_pct = (float)clustered_blocks / active_blocks * 100.0f;

    // Training period: first N frames build background model, no triggers
    if (frame_counter <= MOTION_EMA_TRAINING) {
        if (frame_counter == MOTION_EMA_TRAINING) {
            Serial.printf("EMA background model trained (%d frames, avg brightness=%d)\n",
                          frame_counter, avg_brightness);
        }
        return false;
    }

    // Night mode: instead of blanket suppress, require stronger signal
    // (higher pixel diff threshold + larger cluster) to filter out sensor noise
    // while still detecting actual night motion
    bool is_night = (avg_brightness < MOTION_NIGHT_BRIGHTNESS);
    if (is_night) {
        // Recompute clustered_blocks with stricter neighborhood requirement (3+ neighbors)
        int strict_clustered = 0;
        for (int by2 = 0; by2 < bh; by2++) {
            for (int bx2 = 0; bx2 < bw; bx2++) {
                if (bx2 >= MOTION_GRID_W || by2 >= MOTION_GRID_H) continue;
                if (!block_motion[by2 * MOTION_GRID_W + bx2]) continue;
                int neighbors = 0;
                if (bx2 > 0 && block_motion[by2 * MOTION_GRID_W + (bx2-1)]) neighbors++;
                if (bx2 < bw-1 && bx2+1 < MOTION_GRID_W && block_motion[by2 * MOTION_GRID_W + (bx2+1)]) neighbors++;
                if (by2 > 0 && block_motion[(by2-1) * MOTION_GRID_W + bx2]) neighbors++;
                if (by2 < bh-1 && by2+1 < MOTION_GRID_H && block_motion[(by2+1) * MOTION_GRID_W + bx2]) neighbors++;
                if (neighbors >= 2) strict_clustered++;  // need 2+ neighbors at night
            }
        }
        // Use strict count for night decisions
        motion_score = strict_clustered;
        motion_pct = (float)strict_clustered / active_blocks * 100.0f;
        // Night mode also requires 2.5x trigger percentage (suppress lone-block flicker)
        if (motion_pct < (MOTION_TRIGGER_PCT * 2.5f)) {
            if (motion_detected) {
                motion_detected = false;
                confirm_counter = 0;
            }
            return false;
        }
    }

    // Check lower bound (enough pixels changed?)
    float trigger_pct = MOTION_TRIGGER_PCT * (1.0f + last_agc_gain * 0.05f);
    bool above_lower = (motion_pct >= trigger_pct);

    // Check upper bound (too many pixels = light change, not motion)
    bool below_upper = (motion_pct <= MOTION_UPPER_PCT);

    bool frame_has_motion = above_lower && below_upper;

    // Temporal filter: require MOTION_CONFIRM_FRAMES consecutive frames
    if (frame_has_motion) {
        confirm_counter++;
    } else {
        confirm_counter = 0;
    }

    bool confirmed = (confirm_counter >= MOTION_CONFIRM_FRAMES);

    // Log state changes
    if (confirmed != motion_detected) {
        motion_detected = confirmed;
        if (confirmed) {
            Serial.printf("Motion DETECTED: blocks=%d/%d (%.1f%%), thresh=%.1f%%, blockThresh=%d, gain=%d, bright=%d\n",
                          changed_blocks, active_blocks, motion_pct, trigger_pct, pixel_thresh, last_agc_gain, avg_brightness);
        } else {
            Serial.printf("Motion cleared: blocks=%d (%.1f%%)\n", motion_score, motion_pct);
        }
    }

    return motion_detected;
}

bool MotionDetector::detect(camera_fb_t* fb) {
    if (!bg_gray || !curr_gray || !rgb565_decode_buf || !fb) {
        return false;
    }

    if (millis() - last_check_time < MOTION_CHECK_INTERVAL_MS) {
        return motion_detected;
    }
    last_check_time = millis();

    if (fb->format != PIXFORMAT_JPEG) {
        return false;
    }

    if (!decodeToGrayscale(fb->buf, fb->len, fb->width, fb->height)) {
        return false;
    }

    return compareFrames();
}

bool MotionDetector::detectFromJpeg(const uint8_t* jpeg_data, size_t jpeg_len,
                                     int frame_w, int frame_h) {
    if (!bg_gray || !curr_gray || !rgb565_decode_buf || !jpeg_data || jpeg_len == 0) {
        return false;
    }

    if (millis() - last_check_time < MOTION_CHECK_INTERVAL_MS) {
        return motion_detected;
    }
    last_check_time = millis();

    if (!decodeToGrayscale(jpeg_data, jpeg_len, frame_w, frame_h)) {
        return false;
    }

    return compareFrames();
}

int MotionDetector::getEffectivePixelThreshold() const {
    sensor_t* s = esp_camera_sensor_get();
    int gain = (s != NULL) ? s->status.agc_gain : 0;
    last_agc_gain = gain;
    // Higher gain = more noise = need higher per-pixel threshold
    // gain 0 → 20, gain 10 → 30, gain 20 → 40, gain 30 → 50
    // Plus night boost: low brightness frames need 2.5x to filter sensor noise
    // (instead of blanket suppression — see night mode in detect())
    int threshold = MOTION_PIXEL_THRESHOLD + (int)(gain * 1.0f);
    if (avg_brightness > 0 && avg_brightness < MOTION_NIGHT_BRIGHTNESS) {
        threshold = (int)(threshold * 2.5f);
    }
    return (threshold < 255) ? threshold : 255;
}

bool MotionDetector::isMotionDetected() const {
    return motion_detected;
}

int MotionDetector::getMotionScore() const {
    return motion_score;
}

float MotionDetector::getMotionPercent() const {
    return motion_pct;
}

uint8_t MotionDetector::getAvgBrightness() const {
    return avg_brightness;
}

bool MotionDetector::getBlockMotion(int bx, int by) const {
    if (bx >= 0 && bx < MOTION_GRID_W && by >= 0 && by < MOTION_GRID_H) {
        return block_motion[by * MOTION_GRID_W + bx];
    }
    return false;
}

void MotionDetector::setBlockMask(int bx, int by, bool active) {
    if (bx >= 0 && bx < MOTION_GRID_W && by >= 0 && by < MOTION_GRID_H) {
        block_mask[by * MOTION_GRID_W + bx] = active;
    }
}

bool MotionDetector::getBlockMask(int bx, int by) const {
    if (bx >= 0 && bx < MOTION_GRID_W && by >= 0 && by < MOTION_GRID_H) {
        return block_mask[by * MOTION_GRID_W + bx];
    }
    return true;
}

void MotionDetector::clearMask() {
    memset(block_mask, 1, sizeof(block_mask)); // true = all active
}

int MotionDetector::getGridW() const {
    return width / MOTION_BLOCK_SIZE;
}

int MotionDetector::getGridH() const {
    return height / MOTION_BLOCK_SIZE;
}
