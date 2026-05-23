#include "camera_capture.h"
#include "ESP32-RTSPServer.h"
#include "esp_task_wdt.h" // WDT support
#include "task_priorities.h" // Task priority definitions
#include "motion_detection.h" // Motion Detection
#include "person_detection.h" // Person Detection (AI FOMO)
#include "tracker.h"          // Per-track notification dedup (objectTracker global)
#include "config.h" // Config access
#include "ir_control.h" // readAmbientLightSCCBSafe() for LTR-308 from captureTask
#include "event_log.h"
#include "zone_manager.h"
#include <WiFi.h>

// Extern reference to the global RTSP server instance
extern RTSPServer rtspServer;
extern Config config; // Global config
#ifdef INCLUDE_TELEGRAM
extern bool sendTelegramPhoto(const uint8_t* jpeg_data, size_t jpeg_len, const String& caption, int person_track_id = -1);
extern void sendTelegramNotification(const String& message);
extern void sendTelegramDocument(const char* sdPath, const String& caption);
#else
static inline bool sendTelegramPhoto(const uint8_t*, size_t, const String&, int = -1) { return false; }
static inline void sendTelegramNotification(const String&) {}
static inline void sendTelegramDocument(const char*, const String&) {}
#endif
extern void mqttPublishMotion(bool detected);
extern void mqttPublishPerson(bool detected, float confidence);
extern bool mqttPersonUncertainConnected();
extern void mqttPublishPersonUncertain(float confidence);
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

// Shared timestamp: personDetectionTask writes this when it sends a Telegram photo.
// motionTask reads it to decide whether the AI fallback is still needed.
volatile unsigned long g_lastPersonPhotoTime = 0;

// PERF counters — reset at startup, readable via getPerfXxx() for 30s log in main loop
static volatile uint32_t motion_trigger_count = 0;   // times motion woke personDetectionTask
static volatile uint32_t ai_fallback_count    = 0;   // times AI didn't confirm → fallback photo sent
static volatile uint32_t pd_positive_count    = 0;   // inference runs with person detected
static volatile uint32_t pd_negative_count    = 0;   // inference runs with no person
static volatile uint32_t last_inference_ms    = 0;   // duration of the most recent inference

uint32_t getPerfMotionTriggers()   { return motion_trigger_count; }
uint32_t getPerfAiFallbacks()      { return ai_fallback_count; }
uint32_t getPerfPdPositives()      { return pd_positive_count; }
uint32_t getPerfPdNegatives()      { return pd_negative_count; }
uint32_t getPerfLastInferenceMs()  { return last_inference_ms; }

// Last PersonDecision for /status reporting
volatile uint8_t g_personLastDecision = 0;  // PersonDecision as uint8_t (0=NONE,1=UNCERTAIN,2=CONFIDENT)

// How long motionTask waits for AI confirmation before sending a fallback motion photo.
#define MOTION_AI_FALLBACK_MS 5000  // 5s: enough for one inference cycle (~2.2s) + overhead

// Ring Buffer Globals (CAS-based protocol -- no mutex needed)
FrameBuffer frameBuffers[FRAME_BUFFER_COUNT];
volatile int current_frame_index = -1; // -1 indicates no frame available yet
volatile unsigned long camera_heartbeat = 0;

// Pending sensor settings (defined in camera_capture.h)
volatile int pendingSensorAction = PENDING_FLAG_NONE;
volatile int pendingFrameSize = -1;
PendingSensorSettings pendingSensorValues = {
    PSV_NONE,PSV_NONE, PSV_NONE,PSV_NONE,PSV_NONE,
    PSV_NONE,PSV_NONE, PSV_NONE,PSV_NONE,PSV_NONE,PSV_NONE,
    PSV_NONE,PSV_NONE,PSV_NONE, PSV_NONE,PSV_NONE,PSV_NONE, PSV_NONE
};
SemaphoreHandle_t pendingSensorMutex = NULL;

// Apply any pending sensor settings from HTTP handlers
// Called from captureTask AFTER esp_camera_fb_return() when SCCB bus is free
extern void updateCameraProfile();
static void applyPendingSensorSettings() {
    // Atomic exchange so a second HTTP request that fires between the read
    // and the clear is preserved (it sees PENDING_FLAG_NONE here and overwrites
    // it for the next captureTask iteration).
    int action = __atomic_exchange_n(&pendingSensorAction, PENDING_FLAG_NONE,
                                     __ATOMIC_SEQ_CST);
    if (action == PENDING_FLAG_NONE) return;

    sensor_t* s = esp_camera_sensor_get();
    if (!s) return;

    if (action == PENDING_FLAG_PROFILE) {
        // Force profile re-application (called from loop() via flag)
        updateCameraProfile();
        Serial.println("📷 Profile applied from captureTask (SCCB safe)");
    } else if (action == PENDING_FLAG_FRAMESIZE) {
        int fs = __atomic_exchange_n(&pendingFrameSize, -1, __ATOMIC_SEQ_CST);
        Serial.printf("📷 Framesize %d saved (reboot required to apply)\n", fs);
    } else if (action == PENDING_FLAG_SETTINGS) {
        // Copy pending values under mutex, then apply — SCCB bus is free here
        if (!pendingSensorMutex) return;
        if (xSemaphoreTake(pendingSensorMutex, 0) != pdTRUE) {
            // Mutex busy (HTTP handler writing) — retry next frame
            __atomic_store_n(&pendingSensorAction, PENDING_FLAG_SETTINGS, __ATOMIC_SEQ_CST);
            return;
        }
        PendingSensorSettings p = pendingSensorValues;
        // Clear all pending fields
        pendingSensorValues = {
            PSV_NONE,PSV_NONE, PSV_NONE,PSV_NONE,PSV_NONE,
            PSV_NONE,PSV_NONE, PSV_NONE,PSV_NONE,PSV_NONE,PSV_NONE,
            PSV_NONE,PSV_NONE,PSV_NONE, PSV_NONE,PSV_NONE,PSV_NONE, PSV_NONE
        };
        xSemaphoreGive(pendingSensorMutex);

        if (!s) return;
        #define APPLY(field, fn) if (p.field != PSV_NONE) { fn; }
        APPLY(vflip,       s->set_vflip(s, p.vflip);        Serial.printf("📷 vflip=%d\n", p.vflip))
        APPLY(hmirror,     s->set_hmirror(s, p.hmirror);    Serial.printf("📷 hmirror=%d\n", p.hmirror))
        APPLY(brightness,  s->set_brightness(s, p.brightness))
        APPLY(contrast,    s->set_contrast(s, p.contrast))
        APPLY(saturation,  s->set_saturation(s, p.saturation))
        APPLY(sharpness,   s->set_sharpness(s, p.sharpness))
        APPLY(denoise,     s->set_denoise(s, p.denoise))
        APPLY(aec,         s->set_exposure_ctrl(s, p.aec))
        APPLY(aec2,        s->set_aec2(s, p.aec2))
        APPLY(ae_level,    s->set_ae_level(s, p.ae_level))
        APPLY(aec_value,   s->set_aec_value(s, p.aec_value))
        APPLY(agc,         s->set_gain_ctrl(s, p.agc))
        APPLY(agc_gain,    s->set_agc_gain(s, p.agc_gain))
        APPLY(gainceiling, s->set_gainceiling(s, (gainceiling_t)p.gainceiling))
        APPLY(awb,         s->set_whitebal(s, p.awb))
        APPLY(awb_gain,    s->set_awb_gain(s, p.awb_gain))
        APPLY(wb_mode,     s->set_wb_mode(s, p.wb_mode))
        APPLY(quality,     s->set_quality(s, p.quality))
        #undef APPLY
    }
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

                // Tamper detection — checked every ~5s (150 frames @ 30fps)
                {
                    static int dark_count = 0;
                    static unsigned long last_tamper_alert = 0;
                    const unsigned long TAMPER_COOLDOWN_MS = 10UL * 60UL * 1000UL; // 10 min

                    uint8_t bright = motionDetector.getAvgBrightness();
                    float lux = 0.0f;
                    readAmbientLight(&lux);
                    IRConfig irCfg = getIRConfig();

                    // Camera covered: brightness near zero AND motion_score near zero
                    // (covered lens = uniform black = no noise → score 0)
                    // Genuine dark scene always has sensor noise → score > 0 even without movement
                    int mscore = motionDetector.getMotionScore();
                    if (bright < 6 && mscore < 3) dark_count++;
                    else dark_count = 0;

                    bool tamper = false;
                    const char* reason = "";

                    if (dark_count >= 6) {   // 6 × 5s = 30s black + silent
                        tamper = true;
                        reason = "camera_covered";
                        dark_count = 0;
                    }

                    // WiFi critically weak
                    if (WiFi.status() == WL_CONNECTED && WiFi.RSSI() < -87) {
                        tamper = true;
                        reason = "weak_wifi";
                    }

                    if (tamper && (millis() - last_tamper_alert > TAMPER_COOLDOWN_MS)) {
                        last_tamper_alert = millis();
                        logEvent(EVT_TAMPER, reason);
                        sendTelegramNotification("⚠️ Tamper alert: " + String(reason));
                    }
                }
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
    uint8_t* frameCopy = nullptr;
    bool motionDisabledReported = false;

    while (true) {
        // Wait for notification from captureTask (blocks until new frame)
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(1000)); // 1s timeout to avoid permanent block

        // MQTT reconnect + keepalive (moved here from loop() which is starved by captureTask on Core 1)
        mqttLoop();

        if (!config.motion_detection_enabled) {
            if (frameCopy) {
                free(frameCopy);
                frameCopy = nullptr;
            }
            if (!motionDisabledReported) {
                mqttPublishMotion(false);
                Serial.println("🔕 Firmware motion detection disabled; skipping JPEG motion analysis");
                motionDisabledReported = true;
            }
            continue;
        }
        motionDisabledReported = false;

        // PSRAM copy buffer: copy frame from ring buffer immediately, release slot,
        // then run motion detection + SD/Telegram ops from copy (prevents ring buffer starvation)
        if (!frameCopy) {
            frameCopy = (uint8_t*)ps_malloc(FRAME_BUFFER_SLOT_SIZE);
            if (!frameCopy) {
                Serial.println("❌ Motion task: failed to allocate PSRAM copy buffer!");
                vTaskDelay(pdMS_TO_TICKS(5000));
                continue;
            }
        }

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
        static bool lastMotionState = false;
        static unsigned long aiFallbackArmTime = 0;  // when AI was triggered for fallback
        static bool aiFallbackArmed = false;

        if (motionDetector.isMotionDetected() && !lastMotionState) {
            char evd[EVENT_DETAIL_LEN];
            snprintf(evd, sizeof(evd), "%.1f%%", motionDetector.getMotionPercent());
            logEvent(EVT_MOTION, evd);
        }
        lastMotionState = motionDetector.isMotionDetected();

        if (motionDetector.isMotionDetected() && isInActiveHours()) {
            unsigned long now = millis();

            bool personGateReady = config.person_detection_enabled &&
                                   personDetectionTaskHandle &&
                                   personDetector.isReady();

            if (personGateReady) {
                xTaskNotifyGive(personDetectionTaskHandle);
                motion_trigger_count++;

                // Arm fallback on the first trigger within a fresh cooldown window.
                if (!aiFallbackArmed &&
                    (now - lastTelegramPhoto) > (unsigned long)(config.motion_telegram_cooldown * 1000)) {
                    aiFallbackArmed = true;
                    aiFallbackArmTime = now;
                }

                // Fallback: AI did not confirm a person within MOTION_AI_FALLBACK_MS → send motion photo.
                if (config.motion_telegram_photo && aiFallbackArmed &&
                    (now - aiFallbackArmTime) >= MOTION_AI_FALLBACK_MS &&
                    g_lastPersonPhotoTime < aiFallbackArmTime) {
                    aiFallbackArmed = false;
                    lastTelegramPhoto = now;
                    ai_fallback_count++;
                    Serial.println("📷 AI fallback: no person confirmed, sending motion photo");
                    vTaskDelay(pdMS_TO_TICKS(300));
                    int fresh = getLatestFrameIndex();
                    if (fresh >= 0 && acquireFrameReader(fresh)) {
                        String caption = "Motion detected (no person confirmed)\n(" +
                                         String(motionDetector.getMotionPercent(), 1) + "% of pixels)";
                        char zonesBuf[64] = {0};
                        getActiveZones(motionDetector.getBlockMotionGrid(),
                                       motionDetector.getGridW(), motionDetector.getGridH(),
                                       zonesBuf, sizeof(zonesBuf));
                        if (zonesBuf[0]) caption += "\nZone: " + String(zonesBuf);
                        sendTelegramPhoto(frameBuffers[fresh].data, frameBuffers[fresh].len, caption);
                        releaseFrameReader(fresh);
                    }
                }
            }

            // Direct motion photo when AI gate is not available (person detection off / not ready).
            if (config.motion_telegram_photo && !personGateReady) {
                if (now - lastTelegramPhoto > (unsigned long)(config.motion_telegram_cooldown * 1000)) {
                    lastTelegramPhoto = now;
                    vTaskDelay(pdMS_TO_TICKS(300));
                    int fresh = getLatestFrameIndex();
                    if (fresh >= 0 && acquireFrameReader(fresh)) {
                        String caption = "Motion detected! (" + String(motionDetector.getMotionPercent(), 1) + "% of pixels)";
                        char zonesBuf[64] = {0};
                        getActiveZones(motionDetector.getBlockMotionGrid(),
                                       motionDetector.getGridW(), motionDetector.getGridH(),
                                       zonesBuf, sizeof(zonesBuf));
                        if (zonesBuf[0]) caption += "\nZone: " + String(zonesBuf);
                        sendTelegramPhoto(frameBuffers[fresh].data, frameBuffers[fresh].len, caption);
                        releaseFrameReader(fresh);
                    }
                }
            }
        } else {
            aiFallbackArmed = false;  // disarm when motion stops or outside active hours
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
    const int IDLE_SCAN_MS = 5000;   // catch slow/stationary arrivals missed by motion

    // Local PSRAM copy so we don't hold a ring buffer reader for the full
    // ~3.5s inference window (would starve captureTask with only 3 slots).
    uint8_t* pdFrameCopy = (uint8_t*)ps_malloc(FRAME_BUFFER_SLOT_SIZE);
    if (!pdFrameCopy) {
        Serial.println("❌ PD task: failed to allocate PSRAM copy buffer!");
        personDetectionTaskHandle = NULL;  // Clear stale handle before deleting task
        vTaskDelete(NULL);
        return;
    }

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
        // - If presence active and under miss limit → timeout = person_recheck_interval
        //   (separate from photo cooldown — clears stale presence faster, was 60s before)
        // - Otherwise → periodic idle scan, so motion is not the only AI trigger
        TickType_t waitTicks;
        if (presenceActive && consecutiveMisses < MAX_MISSES) {
            int recheckMs = config.person_recheck_interval * 1000;
            if (recheckMs < 5000) recheckMs = 5000; // minimum 5s
            waitTicks = pdMS_TO_TICKS(recheckMs);
        } else {
            waitTicks = pdMS_TO_TICKS(IDLE_SCAN_MS);
        }

        uint32_t notified = ulTaskNotifyTake(pdTRUE, waitTicks);

        bool isRecheck = !notified && presenceActive;
        bool isIdleScan = !notified && !presenceActive;
        if (notified) {
            Serial.println("PD: woke from motion trigger");
        } else if (isRecheck) {
            Serial.printf("PD: presence re-check (miss %d/%d)\n", consecutiveMisses, MAX_MISSES);
        } else if (isIdleScan) {
            Serial.println("PD: idle scan");
        }

        int idx = current_frame_index;
        if (idx < 0) continue;

        // Acquire reader reference (skip if writer active)
        if (!acquireFrameReader(idx)) continue;

        // FAST: copy frame data + metadata, release ring buffer immediately
        size_t copiedLen = frameBuffers[idx].len;
        uint16_t copiedWidth = frameBuffers[idx].width;
        uint16_t copiedHeight = frameBuffers[idx].height;
        if (copiedLen > 0 && copiedLen <= FRAME_BUFFER_SLOT_SIZE) {
            memcpy(pdFrameCopy, frameBuffers[idx].data, copiedLen);
        } else {
            copiedLen = 0;
        }
        releaseFrameReader(idx);

        if (copiedLen == 0) continue;

        // SLOW: run inference from local copy (ring buffer is free for captureTask)
        PersonDetectionResult result = personDetector.detectFromJpeg(
            pdFrameCopy, copiedLen, copiedWidth, copiedHeight);

        last_inference_ms = (uint32_t)result.inference_ms;

        // Classify detection into three states
        PersonDecision pd_decision;
        if (!result.detected || result.confidence < config.person_confidence_threshold) {
            pd_decision = PersonDecision::NONE;
        } else if (result.confidence >= PERSON_CONFIDENT_THRESHOLD) {
            pd_decision = PersonDecision::CONFIDENT;
        } else {
            pd_decision = PersonDecision::UNCERTAIN;
        }
        g_personLastDecision = (uint8_t)pd_decision;

        // --- CONFIDENT or UNCERTAIN: person above minimum threshold ---
        if (pd_decision != PersonDecision::NONE) {
            pd_positive_count++;
            consecutiveMisses = 0;
            presenceActive = true;

            if (pd_decision == PersonDecision::CONFIDENT) {
                unsigned long now = millis();
                int unnotifiedTrackId = objectTracker.getNextUnnotified();
                bool cooldownPassed = (now - lastPersonPhoto > (unsigned long)(config.person_detection_cooldown * 1000));

                if (unnotifiedTrackId > 0 && cooldownPassed) {
                    lastPersonPhoto = now;

                    Serial.printf("PD: CONFIDENT track_id=%d conf=%.1f%% dets=%d infer=%lums\n",
                                  unnotifiedTrackId, result.confidence * 100,
                                  result.num_detections, result.inference_ms);

                    mqttPublishPerson(true, result.confidence);
                    char evd[EVENT_DETAIL_LEN];
                    snprintf(evd, sizeof(evd), "conf=%.0f%% id=#%d", result.confidence * 100, unnotifiedTrackId);
                    logEvent(EVT_PERSON, evd);

                    if (config.person_telegram_photo && isInActiveHours()) {
                        String caption = "Person detected\n";
                        caption += "Confidence: " + String(result.confidence * 100, 0) + "%\n";
                        caption += "Subject ID: #" + String(unnotifiedTrackId) +
                                   " (new tracked person - not re-alerted while in view)";
                        bool photoSent = sendTelegramPhoto(pdFrameCopy, copiedLen, caption, unnotifiedTrackId);
                        if (photoSent) {
                            g_lastPersonPhotoTime = millis();
                            Serial.println("PD: Telegram photo queued");
                        } else {
                            Serial.println("PD: Telegram photo queue failed");
                        }
                    } else {
                        Serial.println("PD: CONFIDENT (photo skipped: inactive hours or disabled)");
                    }
                } else if (unnotifiedTrackId > 0) {
                    unsigned long remaining = (unsigned long)(config.person_detection_cooldown * 1000) - (now - lastPersonPhoto);
                    Serial.printf("PD: CONFIDENT track #%d cooldown active (%lus remaining)\n",
                                  unnotifiedTrackId, remaining / 1000);
                } else if (isRecheck) {
                    Serial.printf("PD: CONFIDENT still present (conf=%.1f%% infer=%lums)\n",
                                  result.confidence * 100, result.inference_ms);
                }
            } else {
                // UNCERTAIN: above minimum threshold but below CONFIDENT (75%)
                // Send Telegram with low-confidence caption so user can judge.
                // Uses same track dedup + cooldown as CONFIDENT to avoid flooding.
                unsigned long now = millis();
                int unnotifiedTrackId = objectTracker.getNextUnnotified();
                bool cooldownPassed = (now - lastPersonPhoto > (unsigned long)(config.person_detection_cooldown * 1000));

                Serial.printf("PD: UNCERTAIN conf=%.1f%% infer=%lums\n",
                              result.confidence * 100, result.inference_ms);

                if (unnotifiedTrackId > 0 && cooldownPassed) {
                    lastPersonPhoto = now;
                    if (mqttPersonUncertainConnected()) {
                        // A12 integration: route UNCERTAIN through MQTT → YOLO verification
                        mqttPublishPersonUncertain(result.confidence);
                        g_lastPersonPhotoTime = millis();
                        Serial.printf("PD: UNCERTAIN → MQTT (conf=%.1f%% track=#%d)\n",
                                      result.confidence * 100, unnotifiedTrackId);
                    } else if (config.person_telegram_photo && isInActiveHours()) {
                        // Fallback: no MQTT → send Telegram directly
                        String caption = "Possible person (low confidence)\n";
                        caption += "Confidence: " + String(result.confidence * 100, 0) + "% (threshold <75%)\n";
                        caption += "Subject ID: #" + String(unnotifiedTrackId);
                        bool photoSent = sendTelegramPhoto(pdFrameCopy, copiedLen, caption, unnotifiedTrackId);
                        if (photoSent) {
                            g_lastPersonPhotoTime = millis();
                            Serial.println("PD: UNCERTAIN Telegram photo queued (MQTT unavailable)");
                        }
                    }
                }
            }
        }
        // --- NONE: no person above minimum threshold ---
        else {
            pd_negative_count++;
            if (presenceActive) {
                consecutiveMisses++;
                Serial.printf("PD: NONE (miss %d/%d infer=%lums)\n",
                              consecutiveMisses, MAX_MISSES, result.inference_ms);

                if (consecutiveMisses >= MAX_MISSES) {
                    Serial.println("PD: Presence cleared → waiting for motion");
                    mqttPublishPerson(false, 0);
                    presenceActive = false;
                    consecutiveMisses = 0;
                    g_personLastDecision = 0;
                }
            } else {
                Serial.printf("PD: NONE (infer=%lums)\n", result.inference_ms);
            }
        }
    }
}

void startCameraCaptureTask() {
    if (!pendingSensorMutex) pendingSensorMutex = xSemaphoreCreateMutex();

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
