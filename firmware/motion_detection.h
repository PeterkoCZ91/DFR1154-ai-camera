#ifndef MOTION_DETECTION_H
#define MOTION_DETECTION_H

#include "esp_camera.h"
#include <Arduino.h>

// Timing
#define MOTION_CHECK_INTERVAL_MS  100    // ms between analysis runs

// Threshold tuning
#define MOTION_PIXEL_THRESHOLD    28     // per-block absolute diff threshold (0-255)
#define MOTION_REL_THRESHOLD      0.12f  // per-block relative diff (fraction of bgModel)
#define MOTION_TRIGGER_PCT        5.0f   // % of grid blocks needed to trigger
#define MOTION_UPPER_PCT          50.0f  // % above which = global light change, reject
#define MOTION_CONFIRM_FRAMES     3      // consecutive frames required to confirm
#define MOTION_EMA_ALPHA_DAY      0.95f  // EMA background blend factor (day, non-motion blocks)
#define MOTION_EMA_ALPHA_NIGHT    0.98f  // EMA background blend factor (night, slower)
#define MOTION_EMA_ALPHA_MOTION   0.998f // very slow update for motion blocks (prevents stale bg)
#define MOTION_EMA_TRAINING       20     // frames to train background before triggering
#define MOTION_NIGHT_BRIGHTNESS   10     // avg block brightness below this = night
#define MOTION_BLOCK_SIZE         4      // 4×4 px per block

// Fixed pipeline dimensions (decode at 1/8, box-filter to 80×60, grid 20×15)
// Matching M5Stack so zone coordinates are compatible between both projects.
#define MOTION_DECODE_W           80
#define MOTION_DECODE_H           60
#define MOTION_DECODE_MAX_W       256    // largest decoded frame at JPG_SCALE_8X (QXGA/8)
#define MOTION_DECODE_MAX_H       200
#define MOTION_GRID_W             20     // MOTION_DECODE_W / MOTION_BLOCK_SIZE
#define MOTION_GRID_H             15     // MOTION_DECODE_H / MOTION_BLOCK_SIZE

class MotionDetector {
public:
    MotionDetector();
    ~MotionDetector();

    bool init();
    bool detect(camera_fb_t* fb);
    bool detectFromJpeg(const uint8_t* jpeg_data, size_t jpeg_len, int frame_w, int frame_h);

    bool    isMotionDetected()  const;
    int     getMotionScore()    const;
    float   getMotionPercent()  const;
    int     getEffectivePixelThreshold() const;
    uint8_t getAvgBrightness()  const;

    bool        getBlockMotion(int bx, int by) const;
    const bool* getBlockMotionGrid()           const { return block_motion; }

    void setBlockMask(int bx, int by, bool active);
    bool getBlockMask(int bx, int by)          const;
    void clearMask();
    int  getGridW() const { return MOTION_GRID_W; }
    int  getGridH() const { return MOTION_GRID_H; }

private:
    // PSRAM buffers (allocated in init)
    uint8_t* rgb565_buf;  // MOTION_DECODE_MAX_W * MOTION_DECODE_MAX_H * 2 bytes
    uint8_t* gray_buf;    // MOTION_DECODE_W * MOTION_DECODE_H bytes

    // Block-level model — small enough to live in regular heap
    float   bgModel[MOTION_GRID_W * MOTION_GRID_H];
    uint8_t blockGrid[MOTION_GRID_W * MOTION_GRID_H];
    uint8_t diffMask[MOTION_GRID_W * MOTION_GRID_H];
    uint8_t prevDiffMask[MOTION_GRID_W * MOTION_GRID_H];

    bool block_motion[MOTION_GRID_W * MOTION_GRID_H];
    bool block_mask  [MOTION_GRID_W * MOTION_GRID_H];

    bool          motion_detected;
    int           motion_score;
    float         motion_pct;
    int           confirm_counter;
    int           frame_counter;
    uint8_t       avg_brightness;
    uint8_t       prev_brightness;
    unsigned long last_check_time;
    mutable int   last_agc_gain;

    // Derive JPG_SCALE_8X decoded dimensions from capture resolution
    void decodedDims(int frame_w, int frame_h, int& decW, int& decH) const;

    bool decodeAndSubsample(const uint8_t* jpeg_data, size_t jpeg_len,
                            int frame_w, int frame_h);
    void computeBlocks();
    bool compareBlocks();
};

extern MotionDetector motionDetector;

#endif // MOTION_DETECTION_H
