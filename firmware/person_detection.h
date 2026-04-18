#ifndef PERSON_DETECTION_H
#define PERSON_DETECTION_H

#include <Arduino.h>

// FOMO model input dimensions (Edge Impulse 64x64 grayscale, int8 quantized)
#define PD_INPUT_WIDTH   64
#define PD_INPUT_HEIGHT  64
#define PD_ARENA_SIZE    (140 * 1024)  // 140KB tensor arena (model needs ~134KB)
#define PD_MIN_DETECTIONS 2            // Min FOMO centroids to confirm person (1=noisy, 2+=reliable)
#define PD_TEMPORAL_FRAMES 2           // Require N consecutive frames with detection to confirm
                                        // (eliminates ~90% of FOMO false positives, +~100ms latency)

// JPEG decode scale: JPG_SCALE_4X = 1/4 resolution (UXGA→400x300, person~16px on 64x64)
// Bigger than JPG_SCALE_8X (200x150, person~8px) — 4x more pixels for better AI detail
#define PD_DECODE_SCALE  4
#define PD_DECODE_BUF_SIZE (400 * 300 * 2)  // Max decoded size: UXGA/4 = 400x300 x 2B (RGB565) = 240KB

// Result of a single inference run
struct PersonDetectionResult {
    bool detected;          // true if person found above threshold
    float confidence;       // highest confidence score (0.0 - 1.0)
    int num_detections;     // number of bounding boxes above threshold
    unsigned long inference_ms;  // inference duration in milliseconds
};

class PersonDetector {
public:
    PersonDetector();
    ~PersonDetector();

    // Allocate buffers (PSRAM + internal). Call once at startup.
    bool init();

    // Run detection on a JPEG frame from the ring buffer.
    PersonDetectionResult detectFromJpeg(const uint8_t* jpeg_data, size_t jpeg_len,
                                          int frame_w, int frame_h);

    // Get result of last inference
    PersonDetectionResult getLastResult() const { return last_result; }

    // Check if init() succeeded and buffers are allocated
    bool isReady() const { return initialized; }

private:
    // JPEG decode + resize + Edge Impulse FOMO inference
    bool prepareInput(const uint8_t* jpeg_data, size_t jpeg_len, int frame_w, int frame_h);
    PersonDetectionResult runInference();

    // Histogram normalization: 1%/99% percentile stretch on input_buffer (4096 bytes)
    // Improves low-light AI performance where pixels cluster in narrow range (40-90/255)
    void normalizeContrast();

    // Buffers
    uint8_t* grayscale_buffer;  // PD_DECODE_BUF_SIZE = 240KB in PSRAM (JPEG→RGB565 at 1/4 scale)
    uint8_t* input_buffer;      // 64*64 = 4KB in internal SRAM (model input)
    uint8_t* tensor_arena;      // 140KB in PSRAM (TFLite arena, kept as fallback)

    PersonDetectionResult last_result;
    bool initialized;
    int consecutive_detections;  // Temporal filter: count of consecutive detected frames
};

// Global instance
extern PersonDetector personDetector;

// Task handle (used by motionTask to notify personDetectionTask)
extern TaskHandle_t personDetectionTaskHandle;

#endif // PERSON_DETECTION_H
