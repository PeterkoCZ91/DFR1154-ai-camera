#include "config.h"

#ifdef INCLUDE_TIMELAPSE

#include "timelapse.h"
#include "camera_capture.h"
#include "ws_log.h"
#include "SD.h"
#include <time.h>

extern Config config;

static TaskHandle_t timelapseTaskHandle = NULL;

String getPerDayDir() {
    // Arduino SD library mounts at root "/", not "/sdcard". Earlier versions
    // emitted "/sdcard/..." which silently failed every write.
    struct tm timeinfo;
    if (!getLocalTime(&timeinfo, 0)) {
        return String("/");
    }

    char dirName[16];
    snprintf(dirName, sizeof(dirName), "/%04d%02d%02d",
             timeinfo.tm_year + 1900, timeinfo.tm_mon + 1, timeinfo.tm_mday);

    if (!SD.exists(dirName)) {
        if (SD.mkdir(dirName)) {
            wsLog("SD: Created directory %s", dirName);
        } else {
            Serial.printf("SD: Failed to create %s\n", dirName);
            return String("/");
        }
    }

    return String(dirName);
}

static void timelapseTask(void* param) {
    wsLog("Time-lapse task started (interval: %ds)", config.timelapse_interval_sec);

    // Copy buffer for ring buffer frame
    uint8_t* frameCopy = (uint8_t*)ps_malloc(FRAME_BUFFER_SLOT_SIZE);
    if (!frameCopy) {
        wsLog("Time-lapse: PSRAM alloc failed!");
        vTaskDelete(NULL);
        return;
    }

    while (true) {
        // Wait for the configured interval
        vTaskDelay(pdMS_TO_TICKS(config.timelapse_interval_sec * 1000));

        if (!config.timelapse_enabled) continue;

        // SD is mounted once in startCameraServer(); calling SD.begin() here
        // re-inits with the default CS pin and breaks concurrent recordings.
        if (SD.cardSize() == 0) continue;

        // Skip capture if SD card is near full — avoids half-written files
        // and gives the user (or sd_max_usage_percent cleanup) a chance to
        // free space before we wedge the card.
        uint64_t total = SD.totalBytes();
        uint64_t used = SD.usedBytes();
        if (total > 0 && (used * 100 / total) >= 98) {
            static unsigned long last_warn = 0;
            if (millis() - last_warn > 60000) {
                wsLog("Time-lapse: SD full (%llu/%llu), skipping", used, total);
                last_warn = millis();
            }
            continue;
        }

        // Get latest frame from ring buffer
        int idx = getLatestFrameIndex();
        if (idx < 0) continue;

        if (!acquireFrameReader(idx)) continue;

        size_t copiedLen = frameBuffers[idx].len;
        if (copiedLen > 0 && copiedLen <= FRAME_BUFFER_SLOT_SIZE) {
            memcpy(frameCopy, frameBuffers[idx].data, copiedLen);
        } else {
            copiedLen = 0;
        }
        releaseFrameReader(idx);

        if (copiedLen == 0) continue;

        // Build filename: /sdcard/YYYYMMDD/tl_HHMMSS.jpg
        struct tm timeinfo;
        if (!getLocalTime(&timeinfo, 0)) continue;

        String dir = getPerDayDir();
        char filename[64];
        snprintf(filename, sizeof(filename), "%s/tl_%02d%02d%02d.jpg",
                 dir.c_str(), timeinfo.tm_hour, timeinfo.tm_min, timeinfo.tm_sec);

        // Write JPEG to SD
        File f = SD.open(filename, FILE_WRITE);
        if (f) {
            f.write(frameCopy, copiedLen);
            f.close();
            wsLog("Time-lapse: saved %s (%u bytes)", filename, (unsigned)copiedLen);
        } else {
            Serial.printf("Time-lapse: failed to write %s\n", filename);
        }
    }
}

void startTimelapseTask() {
    if (timelapseTaskHandle != NULL) return;

    xTaskCreatePinnedToCore(
        timelapseTask,
        "Timelapse",
        4096,
        NULL,
        1,                    // Low priority
        &timelapseTaskHandle,
        0                     // Core 0
    );
}

void setTimelapseEnabled(bool enabled) {
    config.timelapse_enabled = enabled;
    wsLog("Time-lapse %s", enabled ? "enabled" : "disabled");
}

bool getTimelapseEnabled() {
    return config.timelapse_enabled;
}

#endif // INCLUDE_TIMELAPSE
