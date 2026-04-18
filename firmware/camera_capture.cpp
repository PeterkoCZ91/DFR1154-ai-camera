#include "camera_capture.h"
#include "ESP32-RTSPServer.h"
#include "esp_task_wdt.h" // WDT support
#include "task_priorities.h" // Task priority definitions
#include "motion_detection.h" // Motion Detection
#include "person_detection.h" // Person Detection (AI FOMO)
#include "config.h" // Config access
#include "ir_control.h" // readAmbientLightSCCBSafe() for LTR-308 from captureTask

// Extern reference to the global RTSP server instance
extern RTSPServer rtspServer;
extern Config config; // Global config
extern void sendTelegramPhoto(const uint8_t* jpeg_data, size_t jpeg_len, const String& caption);
extern void sendTelegramDocument(const char* sdPath, const String& caption);
extern void mqttPublishMotion(bool detected);
extern void mqttPublishPerson(bool detected, float confidence);
extern void mqttPublishMotionScore(int score, float percent);
extern void mqttPublishBrightness(uint8_t brightness);
extern void mqttLoop();
extern bool startSDRecording();
extern void stopSDRecording();
extern bool is_recording;
extern char lastRecordingFilename[64];

// Check if current time is within configured active notification hours
static bool isInActiveHours() {
    struct tm timeinfo;
    if (!getLocalTime(&timeinfo, 0)) return true; // NTP not synced → send anyway
    int hour = timeinfo.tm_hour;
    int start = config.motion_notify_start_hour;
    int end = config.motion_notify_end_hour;
    if (start <= end) {
        return hour >= start && hour <= end;       // e.g. 8-22
    } else {
        return hour >= start || hour <= end;        // e.g. 22-7 (wrap midnight)
    }
}

// Ring Buffer Globals (CAS-based protocol -- no mutex needed)
FrameBuffer frameBuffers[FRAME_BUFFER_COUNT];
volatile int current_frame_index = -1; // -1 indicates no frame available yet
volatile unsigned long camera_heartbeat = 0;

// Pending sensor settings (defined in camera_capture.h)
volatile int pendingSensorAction = PENDING_FLAG_NONE;
volatile int pendingFrameSize = -1;

// Apply any pending sensor settings from HTTP handlers
// Called from captureTask AFTER esp_camera_fb_return() when SCCB bus is free
extern void updateCameraProfile();
static void applyPendingSensorSettings() {
    int action = pendingSensorAction;
    if (action == PENDING_FLAG_NONE) return;
    pendingSensorAction = PENDING_FLAG_NONE;

    sensor_t* s = esp_camera_sensor_get();
    if (!s) return;

    if (action == PENDING_FLAG_PROFILE) {
        // Force profile re-application (called from loop() via flag)
        updateCameraProfile();
        Serial.println("📷 Profile applied from captureTask (SCCB safe)");
    } else if (action == PENDING_FLAG_FRAMESIZE) {
        // Framesize change requires DMA reconfiguration — not safe during capture.
        // Value is saved to config.json; takes effect on next reboot.
        int fs = pendingFrameSize;
        pendingFrameSize = -1;
        Serial.printf("📷 Framesize %d saved (reboot required to apply)\n", fs);
    }
    // PENDING_FLAG_SETTINGS: individual settings already applied via sensor API
    // (the API calls work here because SCCB bus is free after fb_return)
}

// Task handles
TaskHandle_t captureTaskHandle = NULL;
static TaskHandle_t motionTaskHandle = NULL;

void captureTask(void* p) {
    Serial.println("📷 Camera Capture Task Started");

    // Initialize heartbeat
    camera_heartbeat = millis();

    // Stack monitoring counter
    static unsigned long frame_count = 0;

    // Precise timing variables
    TickType_t xLastWakeTime;
    const TickType_t xFrequency = pdMS_TO_TICKS(33); // 33ms = ~30 FPS
    xLastWakeTime = xTaskGetTickCount();

    while(true) {
        frame_count++;
        camera_fb_t* fb = esp_camera_fb_get();
        if (fb) {
            // Update heartbeat immediately on success
            camera_heartbeat = millis();

            // 1. Feed RTSP Server (Immediate)
            sensor_t * s = esp_camera_sensor_get();
            int quality = s ? s->status.quality : 10;
            rtspServer.sendRTSPFrame(fb->buf, fb->len, quality, fb->width, fb->height);

            // 2. Motion Detection moved to separate motionTask (lower priority)
            //    captureTask only captures + copies to ring buffer for minimal latency

            // 3. Update Ring Buffer for MJPEG Clients (CAS-based protocol)
            // Find next buffer index using atomic CAS to prevent TOCTOU race
            int next_index = (current_frame_index + 1) % FRAME_BUFFER_COUNT;
            bool found_free = false;

            for (int i = 0; i < FRAME_BUFFER_COUNT; i++) {
                int try_index = (next_index + i) % FRAME_BUFFER_COUNT;
                int expected = 0;
                // Atomically claim slot: 0 (free) -> -1 (writer sentinel)
                // This prevents readers from acquiring the slot during our write
                if (__atomic_compare_exchange_n(&frameBuffers[try_index].ref_count,
                        &expected, -1, false, __ATOMIC_SEQ_CST, __ATOMIC_RELAXED)) {
                    next_index = try_index;
                    found_free = true;
                    break;
                }
            }

            if (found_free) {
                // We exclusively own this slot (ref_count == -1)
                if (fb->len <= FRAME_BUFFER_SLOT_SIZE) {
                    memcpy(frameBuffers[next_index].data, fb->buf, fb->len);
                    frameBuffers[next_index].len = fb->len;
                    frameBuffers[next_index].width = fb->width;
                    frameBuffers[next_index].height = fb->height;
                    frameBuffers[next_index].timestamp = millis();

                    // Release writer lock first, then publish index.
                    // Readers that see the new index are guaranteed to find
                    // ref_count >= 0 (SEQ_CST store provides release semantics).
                    __atomic_store_n(&frameBuffers[next_index].ref_count, 0, __ATOMIC_SEQ_CST);
                    __atomic_store_n(&current_frame_index, next_index, __ATOMIC_SEQ_CST);

                    // Notify motionTask that a new frame is available
                    if (motionTaskHandle) {
                        xTaskNotifyGive(motionTaskHandle);
                    }
                } else {
                    // Frame too large, release the slot
                    __atomic_store_n(&frameBuffers[next_index].ref_count, 0, __ATOMIC_SEQ_CST);
                    Serial.println("⚠️ Frame too large for shared buffer!");
                }
            } else {
                Serial.println("⚠️ All frame buffers busy, skipping MJPEG update");
            }

            // Return frame to driver (releases SCCB bus)
            esp_camera_fb_return(fb);

            // Apply any pending sensor settings while SCCB bus is free
            applyPendingSensorSettings();

            // Periodic LTR-308 ambient light read (~every 5s at 30fps)
            // SCCB bus is free here (after fb_return), safe to do I2C
            // Also run IR auto-mode + camera profile here because loop() is
            // starved by captureTask priority on Core 1
            if (frame_count % CAPTURE_SENSOR_POLL_FRAMES == 0) {
                readAmbientLightSCCBSafe();
                updateIRAutoMode();
                updateCameraProfile();
            }
        } else {
            // FIXED: Update heartbeat even on error to prevent false-positive camera frozen detection
            camera_heartbeat = millis();
            Serial.println("⚠️ Camera capture failed");
            vTaskDelay(pdMS_TO_TICKS(100)); // Wait a bit on error
        }

        // Stack Monitoring (every 1000 frames = ~33 seconds at 30 FPS)
        if (frame_count % 1000 == 0) {
            UBaseType_t stackLeft = uxTaskGetStackHighWaterMark(NULL);
            Serial.printf("📊 CaptureTask stack free: %u bytes (frame: %lu)\n", stackLeft * 4, frame_count);
            if (stackLeft < 512) {  // Less than 2KB free
                Serial.println("⚠️ WARNING: CaptureTask stack running low!");
            }
        }

        // Precise Framerate Control (30 FPS)
        vTaskDelayUntil(&xLastWakeTime, xFrequency);
    }
}

// Motion Detection Task -- runs at lower priority, reads from ring buffer
void motionTask(void* p) {
    Serial.println("🔍 Motion Detection Task Started (separate from capture)");
    unsigned long my_last_timestamp = 0;

    // PSRAM copy buffer: copy frame from ring buffer immediately, release slot,
    // then run motion detection + SD/Telegram ops from copy (prevents ring buffer starvation)
    uint8_t* frameCopy = (uint8_t*)ps_malloc(FRAME_BUFFER_SLOT_SIZE);
    if (!frameCopy) {
        Serial.println("❌ Motion task: failed to allocate PSRAM copy buffer!");
        vTaskDelete(NULL);
        return;
    }

    while (true) {
        // Wait for notification from captureTask (blocks until new frame)
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(1000)); // 1s timeout to avoid permanent block

        int idx = current_frame_index;
        if (idx < 0) continue;

        // Only process if we have a new frame
        if (frameBuffers[idx].timestamp <= my_last_timestamp) continue;

        // Acquire reader reference (skip if writer active)
        if (!acquireFrameReader(idx)) continue;

        // FAST: Copy frame data and release ring buffer immediately
        size_t copiedLen = frameBuffers[idx].len;
        uint16_t copiedWidth = frameBuffers[idx].width;
        uint16_t copiedHeight = frameBuffers[idx].height;
        if (copiedLen <= FRAME_BUFFER_SLOT_SIZE) {
            memcpy(frameCopy, frameBuffers[idx].data, copiedLen);
        } else {
            copiedLen = 0;
        }
        my_last_timestamp = frameBuffers[idx].timestamp;

        releaseFrameReader(idx);
        // Ring buffer slot is now FREE for captureTask

        if (copiedLen == 0) continue;

        // Run motion detection from local copy (ring buffer already released)
        motionDetector.detectFromJpeg(frameCopy, copiedLen, copiedWidth, copiedHeight);

        // SD auto-recording: start on motion, stop after inactivity
        static unsigned long lastMotionTime = 0;
        static bool autoRecordActive = false;
        if (config.sd_auto_record) {
            if (motionDetector.isMotionDetected()) {
                lastMotionTime = millis();
                if (!autoRecordActive && !is_recording) {
                    if (startSDRecording()) {
                        autoRecordActive = true;
                    }
                }
            } else if (autoRecordActive && is_recording) {
                if (millis() - lastMotionTime > (unsigned long)(config.sd_auto_record_duration * 1000)) {
                    char savedFilename[64];
                    strncpy(savedFilename, lastRecordingFilename, sizeof(savedFilename));
                    stopSDRecording();
                    autoRecordActive = false;
                    // Send AVI to Telegram only if motion_telegram_video enabled
                    if (config.motion_telegram_video && savedFilename[0] != '\0') {
                        sendTelegramDocument(savedFilename, "Motion recording (AVI)");
                    }
                }
            }
        }

        // MQTT reconnect + keepalive (moved here from loop() which is starved by captureTask on Core 1)
        mqttLoop();

        // MQTT motion publish with cooldown (max 2x/sec, always publish state changes)
        {
            static unsigned long lastMqttMotion = 0;
            static bool lastMqttMotionState = false;
            bool currentMotion = motionDetector.isMotionDetected();
            unsigned long now_mqtt = millis();

            // Publish on state change immediately, or periodic update with cooldown
            if (currentMotion != lastMqttMotionState || now_mqtt - lastMqttMotion > 500) {
                mqttPublishMotion(currentMotion);
                if (currentMotion) {
                    mqttPublishMotionScore(motionDetector.getMotionScore(), motionDetector.getMotionPercent());
                    mqttPublishBrightness(motionDetector.getAvgBrightness());
                }
                lastMqttMotion = now_mqtt;
                lastMqttMotionState = currentMotion;
            }
        }

        // Motion → Telegram Photo trigger (independent of video flag)
        static unsigned long lastTelegramPhoto = 0;
        if (motionDetector.isMotionDetected() && isInActiveHours()) {
            unsigned long now = millis();

            if (config.person_detection_enabled && personDetectionTaskHandle) {
                // AI has its own cooldown (person_detection_cooldown), no need for motion cooldown here.
                // xTaskNotifyGive is binary — multiple calls just set the flag once.
                Serial.println("PD: motion → notifying AI task");
                xTaskNotifyGive(personDetectionTaskHandle);
            } else if (config.motion_telegram_photo) {
                if (now - lastTelegramPhoto > (unsigned long)(config.motion_telegram_cooldown * 1000)) {
                    lastTelegramPhoto = now;
                    // Wait for sharper follow-up frame, then grab fresh copy
                    vTaskDelay(pdMS_TO_TICKS(300));
                    int fresh = getLatestFrameIndex();
                    if (fresh >= 0 && acquireFrameReader(fresh)) {
                        String caption = "Motion detected! (" + String(motionDetector.getMotionPercent(), 1) + "% of pixels)";
                        sendTelegramPhoto(frameBuffers[fresh].data, frameBuffers[fresh].len, caption);
                        releaseFrameReader(fresh);
                    }
                }
            }
        }
    }
}

// Person Detection Task -- runs AI inference on frames when notified by motionTask
// Supports "presence re-check": after detecting a person, periodically re-runs
// inference even without new motion triggers (handles stationary person whose
// movement has been absorbed into the EMA background model).
void personDetectionTask(void* p) {
    Serial.println("🧠 Person Detection Task Started (AI FOMO)");
    unsigned long lastPersonPhoto = 0;
    bool presenceActive = false;     // true = person recently detected, re-checking
    int consecutiveMisses = 0;       // how many re-checks found no person
    const int MAX_MISSES = 3;        // clear presence after this many misses

    while (true) {
        // Guard: if person detection disabled at runtime, park the task
        if (!config.person_detection_enabled) {
            if (presenceActive) {
                Serial.println("PD: disabled at runtime, clearing presence");
                mqttPublishPerson(false, 0);
                presenceActive = false;
                consecutiveMisses = 0;
            }
            ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
            continue;
        }

        // Determine wait time:
        // - If presence active and under miss limit → timeout = person_detection_cooldown
        // - Otherwise → wait indefinitely for motion trigger
        TickType_t waitTicks;
        if (presenceActive && consecutiveMisses < MAX_MISSES) {
            int cooldownMs = config.person_detection_cooldown * 1000;
            if (cooldownMs < 5000) cooldownMs = 5000; // minimum 5s
            waitTicks = pdMS_TO_TICKS(cooldownMs);
        } else {
            waitTicks = portMAX_DELAY;
        }

        uint32_t notified = ulTaskNotifyTake(pdTRUE, waitTicks);

        bool isRecheck = !notified && presenceActive;
        if (notified) {
            Serial.println("PD: woke from motion trigger");
        } else {
            Serial.printf("PD: presence re-check (miss %d/%d)\n", consecutiveMisses, MAX_MISSES);
        }

        int idx = current_frame_index;
        if (idx < 0) continue;

        // Acquire reader reference (skip if writer active)
        if (!acquireFrameReader(idx)) continue;

        // Run person detection
        PersonDetectionResult result = personDetector.detectFromJpeg(
            frameBuffers[idx].data,
            frameBuffers[idx].len,
            frameBuffers[idx].width,
            frameBuffers[idx].height
        );

        // Release the frame used for inference
        releaseFrameReader(idx);

        // --- Person detected ---
        if (result.detected && result.confidence >= config.person_confidence_threshold) {
            consecutiveMisses = 0;
            presenceActive = true;

            unsigned long now = millis();
            if (now - lastPersonPhoto > (unsigned long)(config.person_detection_cooldown * 1000)) {
                lastPersonPhoto = now;

                if (isRecheck) {
                    Serial.printf("PD: Still present (confidence=%.1f%%, recheck, inference=%lums)\n",
                                  result.confidence * 100, result.inference_ms);
                } else {
                    Serial.printf("PD: Person detected! confidence=%.1f%%, detections=%d, inference=%lums\n",
                                  result.confidence * 100, result.num_detections, result.inference_ms);
                }

                // Publish person event via MQTT
                mqttPublishPerson(true, result.confidence);

                // Send Telegram photo only during active hours and if enabled
                if (config.motion_telegram_photo && isInActiveHours()) {
                    vTaskDelay(pdMS_TO_TICKS(300));

                    int fresh = getLatestFrameIndex();
                    if (fresh >= 0 && acquireFrameReader(fresh)) {
                        String caption = isRecheck
                            ? "Person still present (confidence: " + String(result.confidence * 100, 0) + "%)"
                            : "Person detected! (confidence: " + String(result.confidence * 100, 0) + "%)";
                        sendTelegramPhoto(frameBuffers[fresh].data, frameBuffers[fresh].len, caption);
                        releaseFrameReader(fresh);
                        Serial.println("PD: Telegram photo sent");
                    } else {
                        Serial.println("PD: failed to acquire fresh frame for photo");
                    }
                } else {
                    Serial.println("PD: person confirmed (photo skipped: inactive hours or disabled)");
                }
            } else {
                unsigned long remaining = (unsigned long)(config.person_detection_cooldown * 1000) - (now - lastPersonPhoto);
                Serial.printf("PD: person confirmed, cooldown active (%lus remaining)\n", remaining / 1000);
            }
        }
        // --- No person detected ---
        else {
            if (presenceActive) {
                consecutiveMisses++;
                Serial.printf("PD: No person (miss %d/%d)\n", consecutiveMisses, MAX_MISSES);

                if (consecutiveMisses >= MAX_MISSES) {
                    Serial.println("PD: Presence cleared → waiting for motion");
                    mqttPublishPerson(false, 0);
                    presenceActive = false;
                    consecutiveMisses = 0;
                }
            } else {
                Serial.printf("PD: No person detected (inference=%lums)\n", result.inference_ms);
            }
        }
    }
}

void startCameraCaptureTask() {
    // FIXED: Prevent memory leak by only allocating buffers once
    // Using static flag to ensure buffers are allocated only on first call
    static bool buffers_allocated = false;

    if (!buffers_allocated) {
        // Initialize Ring Buffer (ONE TIME ONLY)
        for (int i = 0; i < FRAME_BUFFER_COUNT; i++) {
            frameBuffers[i].data = (uint8_t*)ps_malloc(FRAME_BUFFER_SLOT_SIZE);
            frameBuffers[i].len = 0;
            frameBuffers[i].ref_count = 0;
            if (!frameBuffers[i].data) {
                Serial.printf("❌ CRITICAL: Failed to allocate PSRAM for frame buffer %d!\n", i);
                return;
            }
        }
        buffers_allocated = true;
        Serial.printf("✅ Allocated %d frame buffers (%dKB each) in PSRAM\n", FRAME_BUFFER_COUNT, FRAME_BUFFER_SLOT_SIZE / 1024);
    } else {
        Serial.println("ℹ️  Frame buffers already allocated (reusing existing buffers)");
    }

    // frameMutex removed: CAS-based ring buffer protocol replaces mutex

    // Initialize Motion Detector
    if (motionDetector.init()) {
        Serial.println("✅ Motion detector initialized (buffers allocated)");
    } else {
        Serial.println("❌ Failed to initialize motion detector (memory issue?)");
    }

    // Start capture task on Core 1 with CRITICAL priority
    xTaskCreatePinnedToCore(
        captureTask,              // Task function
        "CamCapture",             // Task name
        8192,                     // Stack size
        NULL,                     // Parameters
        PRIORITY_CRITICAL,        // Priority (10 - CRITICAL)
        &captureTaskHandle,       // Task handle
        1                         // Core 1 (camera operations)
    );
    Serial.println("📷 Camera Capture Task started with CRITICAL priority (10)");

    // Start motion detection task on Core 0 with LOW priority
    // Separated from captureTask to avoid JPEG decode blocking camera pipeline
    xTaskCreatePinnedToCore(
        motionTask,               // Task function
        "MotionDet",              // Task name
        8192,                     // Stack size (jpg2rgb565 needs stack)
        NULL,                     // Parameters
        PRIORITY_LOW,             // Priority (2 - LOW, background)
        &motionTaskHandle,        // Task handle
        0                         // Core 0 (off camera core)
    );
    Serial.println("🔍 Motion Detection Task started with LOW priority on Core 0");

    // Initialize Person Detector (AI FOMO) -- allocates PSRAM + SRAM buffers
    if (personDetector.init()) {
        Serial.println("✅ Person detector initialized (Phase 1 stub)");
    } else {
        Serial.println("❌ Failed to initialize person detector (memory issue?)");
    }

    // Start person detection task on Core 0 with priority 3 (above motion, below HTTP)
    xTaskCreatePinnedToCore(
        personDetectionTask,          // Task function
        "PersonDet",                  // Task name
        8192,                         // Stack size
        NULL,                         // Parameters
        3,                            // Priority (LOW+1)
        &personDetectionTaskHandle,   // Task handle
        0                             // Core 0 (off camera core)
    );
    Serial.println("🧠 Person Detection Task started with priority 3 on Core 0");
}
