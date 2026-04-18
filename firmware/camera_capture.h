#pragma once
#include "esp_camera.h"
#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

// Ring Buffer Settings
#define FRAME_BUFFER_COUNT 3
#define FRAME_BUFFER_SLOT_SIZE (256 * 1024)  // 256KB per slot in PSRAM

// How often captureTask polls LTR-308, IR auto-mode and camera profile.
// 150 frames @ 30 FPS ≈ 5 seconds — fast enough for sunset transitions,
// slow enough to leave CPU headroom for MJPEG/RTSP streaming.
#define CAPTURE_SENSOR_POLL_FRAMES 150

// Frame Buffer Structure (CAS-based ring buffer protocol)
// ref_count semantics:
//   -1 = writer (captureTask) is writing to this slot
//    0 = slot is free
//   >0 = number of active readers
struct FrameBuffer {
    uint8_t* data;          // Pointer to PSRAM buffer
    size_t len;             // Length of data
    uint16_t width;         // Frame width (for motion detection from ring buffer)
    uint16_t height;        // Frame height
    unsigned long timestamp;// Timestamp of capture
    volatile int ref_count; // CAS-managed: -1=writing, 0=free, >0=readers
};

// Global Ring Buffer
extern FrameBuffer frameBuffers[FRAME_BUFFER_COUNT];
extern volatile int current_frame_index; // Index of the most recent complete frame
extern volatile unsigned long camera_heartbeat; // Heartbeat from capture task

// Task handle (for OTA suspend/resume)
extern TaskHandle_t captureTaskHandle;

// Pending sensor settings: HTTP handlers set flag, captureTask applies when SCCB free
extern volatile int pendingSensorAction;
extern volatile int pendingFrameSize;  // -1 = no change, >= 0 = target FRAMESIZE_xxx
#define PENDING_FLAG_NONE      0
#define PENDING_FLAG_SETTINGS  1
#define PENDING_FLAG_PROFILE   2
#define PENDING_FLAG_FRAMESIZE 3

// Inicializace a spuštění capture tasku
void startCameraCaptureTask();

// --- Ring Buffer Reader Helpers (inline, zero-overhead) ---

// Pokus o získání čtecí reference na frame buffer slot.
// Vrací true pokud se podařilo (ref_count inkrementovaný), false pokud writer drží slot
// nebo pokud po rozumném počtu pokusů stále kolidují jiní readeři.
static inline bool acquireFrameReader(int idx) {
    if (idx < 0 || idx >= FRAME_BUFFER_COUNT) return false;
    int expected = __atomic_load_n(&frameBuffers[idx].ref_count, __ATOMIC_SEQ_CST);
    // Bounded CAS retries; yield mezi pokusy aby jiné tasky mohly postoupit.
    // Writer drží slot jen po dobu memcpy (~2 ms), 16 yieldů = >32 ms → dost času.
    for (int attempt = 0; attempt < 16; attempt++) {
        if (expected < 0) return false; // writer aktivní
        if (__atomic_compare_exchange_n(&frameBuffers[idx].ref_count,
                &expected, expected + 1,
                false, __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST)) {
            return true;
        }
        taskYIELD();
    }
    return false; // příliš mnoho kolizí → caller si zkusí jiný frame
}

// Uvolnění čtecí reference na frame buffer slot.
static inline void releaseFrameReader(int idx) {
    __atomic_fetch_sub(&frameBuffers[idx].ref_count, 1, __ATOMIC_SEQ_CST);
}

// Získání indexu posledního kompletního framu (atomic load, full barrier).
static inline int getLatestFrameIndex() {
    return __atomic_load_n(&current_frame_index, __ATOMIC_SEQ_CST);
}
