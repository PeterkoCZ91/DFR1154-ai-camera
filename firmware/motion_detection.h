#ifndef MOTION_DETECTION_H
#define MOTION_DETECTION_H

#include "esp_camera.h"
#include <Arduino.h>

// Configuration
#define MOTION_CHECK_INTERVAL_MS 100   // Check for motion every 100ms (~10 checks/sec)
#define MOTION_PIXEL_THRESHOLD 20      // Per-pixel grayscale diff threshold (0-255)
#define MOTION_TRIGGER_PCT 5.0f        // % of pixels that must change to trigger
#define MOTION_UPPER_PCT 70.0f         // % above which = global light change, reject
#define MOTION_CONFIRM_FRAMES 2        // Consecutive frames required to confirm motion
#define MOTION_EMA_ALPHA_DAY 0.92f     // EMA weight for day (slow adapt to light changes)
#define MOTION_EMA_ALPHA_NIGHT 0.97f   // EMA weight for night (very slow adapt, reduce noise)
#define MOTION_EMA_TRAINING 15         // First N frames: training period (no triggers)
#define MOTION_NIGHT_BRIGHTNESS 10     // Avg brightness below this = night (suppress detection)
#define MOTION_CLUSTER_MIN 2           // Min adjacent motion blocks to count as real motion
#define MOTION_BLOCK_SIZE 4            // NxN pixels per block (4x4 = 16 pixels, natural low-pass filter)
// Grid sized for UXGA at SCALE_FACTOR=4: 1600/4/4=100, 1200/4/4=75 — round up for safety
#define MOTION_GRID_W 128              // Max block grid width
#define MOTION_GRID_H 96               // Max block grid height

class MotionDetector {
public:
    MotionDetector();
    ~MotionDetector();

    // Initialize the motion detector
    bool init();

    // Process a frame and check for motion
    // Returns true if motion was detected in this frame
    bool detect(camera_fb_t* fb);

    // Process from ring buffer data (no camera_fb_t needed)
    bool detectFromJpeg(const uint8_t* jpeg_data, size_t jpeg_len, int frame_w, int frame_h);

    // Get the current motion status (latched for a short duration or immediate)
    bool isMotionDetected() const;

    // Get the last motion score (number of changed pixels)
    int getMotionScore() const;

    // Get the changed percentage from last frame
    float getMotionPercent() const;

    // Get effective pixel threshold adjusted for AGC gain
    int getEffectivePixelThreshold() const;

    // Get average frame brightness (0-255, for night mode detection)
    uint8_t getAvgBrightness() const;

    // Get per-block motion state from last comparison (true = motion in block)
    bool getBlockMotion(int bx, int by) const;

    // ROI mask: true = active (detect motion), false = masked (ignore)
    void setBlockMask(int bx, int by, bool active);
    bool getBlockMask(int bx, int by) const;
    void clearMask();          // Reset all blocks to active
    int getGridW() const;      // Current block grid width
    int getGridH() const;      // Current block grid height

private:
    uint8_t* bg_gray;            // EMA background model (running average)
    uint8_t* curr_gray;          // Current frame grayscale buffer
    uint8_t* rgb565_decode_buf;  // Temporary RGB565 decode buffer (reused)
    size_t gray_alloc_size;      // Allocated grayscale buffer size
    size_t rgb565_alloc_size;    // Allocated RGB565 buffer size

    bool motion_detected;
    int motion_score;            // Changed pixel count
    float motion_pct;            // Changed pixel percentage
    int confirm_counter;         // Consecutive frames above threshold
    int frame_counter;           // Total frames processed (for EMA training)
    uint8_t avg_brightness;      // Average frame brightness (for night suppression)
    uint8_t prev_brightness;     // Previous frame brightness (for sudden-change detection)
    unsigned long last_check_time;

    // Dimensions for the downscaled comparison frame
    // SCALE_FACTOR=4 → UXGA becomes 400x300 (4x more detail than scale=8)
    const int SCALE_FACTOR = 4;
    int width;
    int height;
    mutable int last_agc_gain;

    // Internal: decode JPEG to RGB565, convert to grayscale
    bool decodeToGrayscale(const uint8_t* jpeg_data, size_t jpeg_len,
                           int frame_w, int frame_h);

    // ROI block mask (true=active, false=masked/ignored)
    bool block_mask[MOTION_GRID_W * MOTION_GRID_H];

    // Per-block motion state from last comparison
    bool block_motion[MOTION_GRID_W * MOTION_GRID_H];

    // Internal: compare curr_gray vs prev_gray, return true if motion confirmed
    bool compareFrames();
};

extern MotionDetector motionDetector;

#endif // MOTION_DETECTION_H
