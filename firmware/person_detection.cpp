#include "person_detection.h"
#include "img_converters.h"
#include "tracker.h"

// Silence the object detection count warning before including EI header
#define SILENCE_EI_CLASSFIER_OBJECT_DETECTION_COUNT_WARNING
#include <Person_detection_FOMO_inferencing.h>

// Global instance
PersonDetector personDetector;
TaskHandle_t personDetectionTaskHandle = NULL;

// Signal callback for Edge Impulse — reads grayscale pixels from input_buffer
static uint8_t* ei_input_buf = nullptr;

static int get_signal_data(size_t offset, size_t length, float *out_ptr) {
    for (size_t i = 0; i < length; i++) {
        uint8_t g = ei_input_buf[offset + i];
        // EI expects pixel as float(0xRRGGBB) — for grayscale all channels equal
        out_ptr[i] = (float)((g << 16) | (g << 8) | g);
    }
    return 0;
}

PersonDetector::PersonDetector()
    : grayscale_buffer(NULL)
    , input_buffer(NULL)
    , tensor_arena(NULL)
    , last_result{false, 0.0f, 0, 0}
    , initialized(false)
    , consecutive_detections(0)
{
}

PersonDetector::~PersonDetector() {
    if (grayscale_buffer) { free(grayscale_buffer); grayscale_buffer = NULL; }
    if (input_buffer)     { free(input_buffer);     input_buffer = NULL; }
    if (tensor_arena)     { free(tensor_arena);     tensor_arena = NULL; }
    initialized = false;
}

bool PersonDetector::init() {
    if (initialized) return true;

    // Allocate decode buffer in PSRAM for JPG_SCALE_4X
    // UXGA/4 = 400x300 x 2B (RGB565) = 240KB, then overwritten in-place with grayscale
    grayscale_buffer = (uint8_t*)ps_malloc(PD_DECODE_BUF_SIZE);
    if (!grayscale_buffer) {
        Serial.println("PersonDetector: Failed to allocate grayscale buffer (PSRAM)");
        return false;
    }

    // Allocate model input buffer in internal SRAM (64×64 = 4096 bytes)
    input_buffer = (uint8_t*)malloc(PD_INPUT_WIDTH * PD_INPUT_HEIGHT);
    if (!input_buffer) {
        Serial.println("PersonDetector: Failed to allocate input buffer (SRAM)");
        free(grayscale_buffer); grayscale_buffer = NULL;
        return false;
    }

    // Allocate tensor arena in PSRAM (kept as reserve, EI SDK manages its own)
    tensor_arena = (uint8_t*)ps_malloc(PD_ARENA_SIZE);
    if (!tensor_arena) {
        Serial.println("PersonDetector: Failed to allocate tensor arena (PSRAM)");
        free(grayscale_buffer); grayscale_buffer = NULL;
        free(input_buffer);     input_buffer = NULL;
        return false;
    }

    initialized = true;
    Serial.printf("PersonDetector: Initialized (decode=%uB, input=%uB, arena=%uB)\n",
                  PD_DECODE_BUF_SIZE, PD_INPUT_WIDTH * PD_INPUT_HEIGHT, PD_ARENA_SIZE);
    Serial.printf("PersonDetector: EI model '%s' %dx%d, labels=%d, arena=%u\n",
                  EI_CLASSIFIER_PROJECT_NAME,
                  EI_CLASSIFIER_INPUT_WIDTH, EI_CLASSIFIER_INPUT_HEIGHT,
                  EI_CLASSIFIER_LABEL_COUNT, EI_CLASSIFIER_TFLITE_LARGEST_ARENA_SIZE);
    return true;
}

// JPEG → RGB565 (JPG_SCALE_4X) → grayscale → bilinear resize to 64×64
bool PersonDetector::prepareInput(const uint8_t* jpeg_data, size_t jpeg_len,
                                   int frame_w, int frame_h) {
    if (!initialized) return false;

    // Step 1: Decode JPEG to RGB565 at 1/4 scale → 400×300 (UXGA) or 256×192 (XGA)
    // Using 1/4 instead of 1/8: 4x more pixels, person ~16px on 64x64 target (was ~8px)
    int scaled_w = frame_w / PD_DECODE_SCALE;
    int scaled_h = frame_h / PD_DECODE_SCALE;
    size_t rgb565_size = scaled_w * scaled_h * 2;

    if (rgb565_size > PD_DECODE_BUF_SIZE) {
        Serial.println("PersonDetector: Scaled frame too large for buffer");
        return false;
    }

    bool converted = jpg2rgb565(jpeg_data, jpeg_len, grayscale_buffer, JPG_SCALE_4X);
    if (!converted) {
        Serial.println("PersonDetector: JPEG decode failed");
        return false;
    }

    // Step 2: RGB565 → grayscale in-place (overwrite grayscale_buffer)
    // RGB565: RRRRRGGGGGGBBBBB (16 bit)
    uint16_t* rgb565 = (uint16_t*)grayscale_buffer;
    uint8_t* gray = grayscale_buffer;  // Write grayscale from start (smaller, safe in-place)
    for (int i = 0; i < scaled_w * scaled_h; i++) {
        uint16_t pixel = rgb565[i];
        uint8_t r = (pixel >> 11) & 0x1F;
        uint8_t g = (pixel >> 5) & 0x3F;
        uint8_t b = pixel & 0x1F;
        // Weighted average with 5/6/5 bit expansion
        gray[i] = (uint8_t)((r * 8 * 77 + g * 4 * 150 + b * 8 * 29) >> 8);
    }

    // Step 3: Bilinear resize grayscale (scaled_w × scaled_h) → input_buffer (64×64)
    int dst_w = PD_INPUT_WIDTH;
    int dst_h = PD_INPUT_HEIGHT;
    for (int dy = 0; dy < dst_h; dy++) {
        float src_y = (float)dy * (scaled_h - 1) / (dst_h - 1);
        int y0 = (int)src_y;
        int y1 = (y0 + 1 < scaled_h) ? y0 + 1 : y0;
        float fy = src_y - y0;

        for (int dx = 0; dx < dst_w; dx++) {
            float src_x = (float)dx * (scaled_w - 1) / (dst_w - 1);
            int x0 = (int)src_x;
            int x1 = (x0 + 1 < scaled_w) ? x0 + 1 : x0;
            float fx = src_x - x0;

            float val = gray[y0 * scaled_w + x0] * (1 - fx) * (1 - fy)
                      + gray[y0 * scaled_w + x1] * fx * (1 - fy)
                      + gray[y1 * scaled_w + x0] * (1 - fx) * fy
                      + gray[y1 * scaled_w + x1] * fx * fy;
            input_buffer[dy * dst_w + dx] = (uint8_t)(val + 0.5f);
        }
    }

    // Step 4: Normalize contrast for low-light conditions
    normalizeContrast();

    return true;
}

// Histogram normalization: 1%/99% percentile min-max stretch on input_buffer
// In low light, pixels cluster in narrow range (e.g. 40-90/255).
// FOMO model was trained on full-range images, so stretching improves detection.
void PersonDetector::normalizeContrast() {
    const int total = PD_INPUT_WIDTH * PD_INPUT_HEIGHT;  // 4096

    // Build histogram (256 bins on stack — only 256 bytes)
    uint16_t hist[256] = {0};
    for (int i = 0; i < total; i++) {
        hist[input_buffer[i]]++;
    }

    // Find 1st and 99th percentile
    int threshold = total / 100;  // 1% = 40 pixels
    int low = 0, high = 255;
    int cumulative = 0;
    for (int i = 0; i < 256; i++) {
        cumulative += hist[i];
        if (cumulative >= threshold) { low = i; break; }
    }
    cumulative = 0;
    for (int i = 255; i >= 0; i--) {
        cumulative += hist[i];
        if (cumulative >= threshold) { high = i; break; }
    }

    // Guard: skip if already good contrast (range > 200) or degenerate
    // Also skip true-night frames (range < 20) — stretching pure dark only
    // amplifies IR sensor noise into false detections.
    int range = high - low;
    if (range > 200 || range < 20) return;

    // Linear stretch: map [low, high] → [0, 255]
    // Use fixed-point: scale = 255 * 256 / range (8.8 fixed point)
    int scale_fp = (255 * 256) / range;
    for (int i = 0; i < total; i++) {
        int val = input_buffer[i];
        if (val <= low) { input_buffer[i] = 0; }
        else if (val >= high) { input_buffer[i] = 255; }
        else { input_buffer[i] = (uint8_t)(((val - low) * scale_fp) >> 8); }
    }
}

// Run Edge Impulse FOMO classifier on input_buffer
PersonDetectionResult PersonDetector::runInference() {
    PersonDetectionResult result;
    result.detected = false;
    result.confidence = 0.0f;
    result.num_detections = 0;
    result.inference_ms = 0;

    // Set up signal callback to read from input_buffer
    ei_input_buf = input_buffer;

    signal_t signal;
    signal.total_length = EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE;
    signal.get_data = &get_signal_data;

    ei_impulse_result_t ei_result = { 0 };
    EI_IMPULSE_ERROR err = run_classifier(&signal, &ei_result, false);

    if (err != EI_IMPULSE_OK) {
        Serial.printf("PersonDetector: run_classifier failed (%d)\n", (int)err);
        return result;
    }

    // Parse FOMO bounding boxes (centroids)
    // Build detection array for tracker: [cx, cy, conf] triples
    float track_dets[16 * 3];  // Max 16 detections per frame
    int detections = 0;
    float max_confidence = 0.0f;

    for (uint32_t i = 0; i < ei_result.bounding_boxes_count && detections < 16; i++) {
        if (ei_result.bounding_boxes[i].value > 0.0f) {
            track_dets[detections * 3 + 0] = (float)ei_result.bounding_boxes[i].x;
            track_dets[detections * 3 + 1] = (float)ei_result.bounding_boxes[i].y;
            track_dets[detections * 3 + 2] = ei_result.bounding_boxes[i].value;
            detections++;
            if (ei_result.bounding_boxes[i].value > max_confidence) {
                max_confidence = ei_result.bounding_boxes[i].value;
            }
        }
    }

    bool raw_detected = (detections >= PD_MIN_DETECTIONS);

    // Update object tracker with current detections (persistent IDs across frames)
    int confirmed_tracks = objectTracker.update(track_dets, detections);

    // Temporal filter: require N consecutive detected frames to confirm
    // Eliminates ~90% of FOMO false positives (noise patches, IR reflections)
    if (raw_detected) {
        if (consecutive_detections < PD_TEMPORAL_FRAMES) consecutive_detections++;
    } else {
        consecutive_detections = 0;
    }

    // Detection is confirmed if: temporal filter passed AND tracker has confirmed tracks
    result.detected = (consecutive_detections >= PD_TEMPORAL_FRAMES) && (confirmed_tracks > 0);
    result.confidence = max_confidence;
    result.num_detections = detections;
    result.inference_ms = (unsigned long)(ei_result.timing.classification);

    return result;
}

PersonDetectionResult PersonDetector::detectFromJpeg(const uint8_t* jpeg_data, size_t jpeg_len,
                                                      int frame_w, int frame_h) {
    if (!initialized) {
        last_result = {false, 0.0f, 0, 0};
        return last_result;
    }

    unsigned long start = millis();

    if (!prepareInput(jpeg_data, jpeg_len, frame_w, frame_h)) {
        // Reset temporal counter on decode failure — otherwise a recovered
        // frame could pass the temporal filter on a single hit and fire a
        // false positive inherited from detections before the corruption.
        consecutive_detections = 0;
        last_result = {false, 0.0f, 0, 0};
        return last_result;
    }

    last_result = runInference();
    last_result.inference_ms = millis() - start;

    return last_result;
}
