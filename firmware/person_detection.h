#ifndef PERSON_DETECTION_H
#define PERSON_DETECTION_H

#include <Arduino.h>

// FOMO model input dimensions (Edge Impulse 64x64 grayscale, int8 quantized)
#define PD_INPUT_WIDTH   64
#define PD_INPUT_HEIGHT  64
#define PD_ARENA_SIZE    (140 * 1024)  // Heap arena size when EI static allocation is disabled
#define PD_MIN_DETECTIONS 1            // Min FOMO centroids to confirm person; temporal filter provides false-positive rejection
#define PD_TEMPORAL_FRAMES 2           // Require N consecutive frames with detection to confirm
                                        // (eliminates ~90% of FOMO false positives, +~100ms latency)

// JPEG decode scale: JPG_SCALE_8X = 1/8 resolution (UXGA→200x150)
// DC-only decode uses ~4x less heap than JPG_SCALE_4X — avoids OOM when TLS is active.
// Final 64x64 FOMO input comes from bilinear resize so quality impact is minimal.
#define PD_DECODE_SCALE  8
#define PD_DECODE_BUF_SIZE (200 * 150 * 2)  // Max decoded size: UXGA/8 = 200x150 x 2B (RGB565) = 60KB

// Result of a single inference run
struct PersonDetectionResult {
    bool detected;          // true if person found above threshold
    float confidence;       // highest confidence score (0.0 - 1.0)
    int num_detections;     // number of bounding boxes above threshold
    unsigned long inference_ms;  // inference duration in milliseconds
};

// Three-state output: NONE → drop, UNCERTAIN → log/A12, CONFIDENT → Telegram
enum class PersonDecision : uint8_t { NONE=0, UNCERTAIN=1, CONFIDENT=2 };
// Confidence at or above this triggers direct Telegram notification
#define PERSON_CONFIDENT_THRESHOLD 0.75f

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
    uint8_t* grayscale_buffer;  // PD_DECODE_BUF_SIZE in PSRAM (JPEG→RGB565 at 1/8 scale)
    uint8_t* input_buffer;      // 64*64 = 4KB in internal SRAM (model input)
    uint8_t* tensor_arena;      // Optional PSRAM reserve when EI static allocation is disabled

    PersonDetectionResult last_result;
    bool initialized;
    int consecutive_detections;  // Temporal filter: count of consecutive detected frames
};

// Global instance
extern PersonDetector personDetector;

// Task handle (used by motionTask to notify personDetectionTask)
extern TaskHandle_t personDetectionTaskHandle;

#endif // PERSON_DETECTION_H
