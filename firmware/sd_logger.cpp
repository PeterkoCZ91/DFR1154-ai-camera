#include "sd_logger.h"
#include "esp_http_server.h"
#include "ArduinoJson.h"
#include <SD.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <esp_heap_caps.h>
#include <time.h>
#include <sys/stat.h>
#include <dirent.h>

// ── State ────────────────────────────────────────────────────────────────────

static SemaphoreHandle_t sdl_mutex = NULL;

static bool      sdl_active       = false;
static uint32_t  sdl_timeout_h    = 0;
static unsigned long sdl_start_ms = 0;
static unsigned long sdl_last_flush_ms = 0;
static uint32_t  sdl_lines_written = 0;
static uint32_t  sdl_bytes_written = 0;
static char      sdl_filepath[64]  = {0};

// RAM write buffer
static char* sdl_buf = NULL;
static size_t sdl_buf_len = 0;

// Status JSON (returned by sdLogStatusJson)
static char sdl_status_json[256];

// ── Internal helpers ─────────────────────────────────────────────────────────

static void buildFilePath(char* out, size_t outlen) {
    time_t now = time(NULL);
    struct tm* t = localtime(&now);
    if (t && t->tm_year > 100) {
        snprintf(out, outlen, "/logs/%04d%02d%02d_%02d%02d%02d.log",
                 t->tm_year + 1900, t->tm_mon + 1, t->tm_mday,
                 t->tm_hour, t->tm_min, t->tm_sec);
    } else {
        // No RTC — use uptime millis
        snprintf(out, outlen, "/logs/boot_%lu.log", millis());
    }
}

static void ensureLogsDir() {
    if (!SD.exists("/logs")) {
        SD.mkdir("/logs");
    }
}

static void flushBufToSD() {
    if (!sdl_buf || sdl_buf_len == 0 || sdl_filepath[0] == '\0') return;

    File f = SD.open(sdl_filepath, FILE_APPEND);
    if (!f) {
        Serial.printf("📋 sdLog: cannot open %s\n", sdl_filepath);
        return;
    }
    f.write((const uint8_t*)sdl_buf, sdl_buf_len);
    f.flush();
    f.close();

    sdl_bytes_written += sdl_buf_len;
    sdl_buf_len = 0;
}

static void cleanOldLogs() {
    DIR* dir = opendir("/sdcard/logs");
    if (!dir) return;

    time_t now = time(NULL);
    uint64_t total_bytes = 0;
    struct dirent* entry;

    // First pass: total size + delete by age
    while ((entry = readdir(dir)) != NULL) {
        if (entry->d_type != DT_REG) continue;
        char path[96];
        snprintf(path, sizeof(path), "/sdcard/logs/%s", entry->d_name);

        struct stat st;
        if (stat(path, &st) != 0) continue;
        total_bytes += st.st_size;

        double age_days = difftime(now, st.st_mtime) / 86400.0;
        if (age_days > SDL_MAX_AGE_DAYS) {
            SD.remove(path);
            Serial.printf("📋 sdLog: removed old log %s (%.0f days)\n", entry->d_name, age_days);
        }
    }
    closedir(dir);

    // Second pass: delete oldest if over size limit
    uint64_t limit_bytes = (uint64_t)SDL_MAX_DIR_MB * 1024 * 1024;
    if (total_bytes <= limit_bytes) return;

    dir = opendir("/sdcard/logs");
    if (!dir) return;

    // Collect files with mtime for sorting (simple: just delete oldest first)
    while ((entry = readdir(dir)) != NULL) {
        if (total_bytes <= limit_bytes) break;
        if (entry->d_type != DT_REG) continue;
        char path[96];
        snprintf(path, sizeof(path), "/sdcard/logs/%s", entry->d_name);
        struct stat st;
        if (stat(path, &st) != 0) continue;
        total_bytes -= st.st_size;
        SD.remove(path);
        Serial.printf("📋 sdLog: removed log %s (size limit)\n", entry->d_name);
    }
    closedir(dir);
}

// ── Public API ───────────────────────────────────────────────────────────────

void sdLogInit(httpd_handle_t server) {
    sdl_mutex = xSemaphoreCreateMutex();
    if (!sdl_buf && psramFound()) {
        sdl_buf = (char*)heap_caps_malloc(SDL_BUF_SIZE, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    }
    if (!sdl_buf) {
        Serial.println("📋 SD Logger buffer unavailable; sessions disabled");
    }

    // Register HTTP handlers
    httpd_uri_t get_uri = {
        .uri     = "/sd-log",
        .method  = HTTP_GET,
        .handler = [](httpd_req_t* req) -> esp_err_t {
            httpd_resp_set_type(req, "application/json");
            httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
            httpd_resp_send(req, sdLogStatusJson(), HTTPD_RESP_USE_STRLEN);
            return ESP_OK;
        },
        .user_ctx = NULL
    };
    httpd_uri_t post_uri = {
        .uri     = "/sd-log",
        .method  = HTTP_POST,
        .handler = [](httpd_req_t* req) -> esp_err_t {
            char body[128] = {0};
            int received = httpd_req_recv(req, body, sizeof(body) - 1);
            if (received <= 0) {
                httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "{}");
                return ESP_OK;
            }
            StaticJsonDocument<128> doc;
            if (deserializeJson(doc, body) != DeserializationError::Ok) {
                httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "{}");
                return ESP_OK;
            }
            const char* action = doc["action"] | "";
            if (strcmp(action, "start") == 0) {
                uint32_t th = doc["timeout_h"] | SDL_DEFAULT_TIMEOUT_H;
                bool ok = sdLogStart(th);
                httpd_resp_set_type(req, "application/json");
                httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
                String resp = ok ? "{\"status\":\"started\",\"file\":\"" + String(sdl_filepath) + "\"}"
                                 : "{\"status\":\"error\",\"reason\":\"already active or SD not ready\"}";
                httpd_resp_sendstr(req, resp.c_str());
            } else if (strcmp(action, "stop") == 0) {
                sdLogStop();
                httpd_resp_set_type(req, "application/json");
                httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
                httpd_resp_sendstr(req, "{\"status\":\"stopped\"}");
            } else if (strcmp(action, "flush") == 0) {
                sdLogFlush();
                httpd_resp_set_type(req, "application/json");
                httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
                httpd_resp_sendstr(req, "{\"status\":\"flushed\"}");
            } else {
                httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "{\"error\":\"unknown action\"}");
            }
            return ESP_OK;
        },
        .user_ctx = NULL
    };

    httpd_register_uri_handler(server, &get_uri);
    httpd_register_uri_handler(server, &post_uri);
    Serial.println("📋 SD Logger ready (/sd-log)");
}

bool sdLogStart(uint32_t timeout_h) {
    if (!sdl_mutex) return false;
    if (!sdl_buf) return false;
    if (sdl_active) return false;
    if (!SD.cardType()) return false;  // SD not mounted

    ensureLogsDir();
    buildFilePath(sdl_filepath, sizeof(sdl_filepath));

    // Write session header
    char header[128];
    snprintf(header, sizeof(header), "=== SD Log session started | timeout=%uh ===\n", timeout_h);

    xSemaphoreTake(sdl_mutex, portMAX_DELAY);
    sdl_active        = true;
    sdl_timeout_h     = timeout_h;
    sdl_start_ms      = millis();
    sdl_last_flush_ms = millis();
    sdl_lines_written = 0;
    sdl_bytes_written = 0;
    sdl_buf_len       = 0;
    size_t hlen = strlen(header);
    memcpy(sdl_buf, header, hlen);
    sdl_buf_len = hlen;
    xSemaphoreGive(sdl_mutex);

    Serial.printf("📋 sdLog: session started → %s (timeout=%uh)\n", sdl_filepath, timeout_h);
    cleanOldLogs();
    return true;
}

void sdLogStop() {
    if (!sdl_mutex || !sdl_active) return;

    xSemaphoreTake(sdl_mutex, portMAX_DELAY);

    // Append footer to buffer before final flush
    char footer[80];
    snprintf(footer, sizeof(footer),
             "=== Session ended | %lu lines | %lu bytes ===\n",
             (unsigned long)sdl_lines_written, (unsigned long)sdl_bytes_written);
    size_t flen = strlen(footer);
    size_t available = SDL_BUF_SIZE - sdl_buf_len;
    if (flen < available) {
        memcpy(sdl_buf + sdl_buf_len, footer, flen);
        sdl_buf_len += flen;
    }

    flushBufToSD();
    sdl_active = false;
    sdl_filepath[0] = '\0';
    xSemaphoreGive(sdl_mutex);

    Serial.printf("📋 sdLog: session stopped (%lu lines)\n", (unsigned long)sdl_lines_written);
}

void sdLogFlush() {
    if (!sdl_mutex || !sdl_active) return;
    xSemaphoreTake(sdl_mutex, portMAX_DELAY);
    flushBufToSD();
    sdl_last_flush_ms = millis();
    xSemaphoreGive(sdl_mutex);
}

void sdLogWrite(const char* line) {
    if (!sdl_active || !sdl_mutex || !sdl_buf || !line) return;
    if (xSemaphoreTake(sdl_mutex, pdMS_TO_TICKS(5)) != pdTRUE) return;

    size_t linelen = strlen(line);
    // +1 for newline
    if (sdl_buf_len + linelen + 1 >= SDL_BUF_SIZE) {
        // Buffer full — flush inline (this is rare, buffer is 4KB)
        flushBufToSD();
        sdl_last_flush_ms = millis();
    }

    memcpy(sdl_buf + sdl_buf_len, line, linelen);
    sdl_buf_len += linelen;
    sdl_buf[sdl_buf_len++] = '\n';
    sdl_lines_written++;
    xSemaphoreGive(sdl_mutex);
}

void sdLogTick() {
    if (!sdl_active) return;

    unsigned long now = millis();

    // Periodic flush
    if (now - sdl_last_flush_ms >= SDL_FLUSH_MS) {
        sdLogFlush();
    }

    // Auto-stop timeout
    if (sdl_timeout_h > 0) {
        unsigned long elapsed_ms = now - sdl_start_ms;
        if (elapsed_ms >= (unsigned long)sdl_timeout_h * 3600000UL) {
            Serial.printf("📋 sdLog: auto-stop (timeout %uh reached)\n", sdl_timeout_h);
            sdLogStop();
        }
    }
}

bool sdLogIsActive() {
    return sdl_active;
}

const char* sdLogStatusJson() {
    unsigned long elapsed_s = sdl_active ? (millis() - sdl_start_ms) / 1000 : 0;
    uint32_t remaining_s = 0;
    if (sdl_active && sdl_timeout_h > 0) {
        unsigned long deadline_ms = sdl_start_ms + (unsigned long)sdl_timeout_h * 3600000UL;
        unsigned long now = millis();
        remaining_s = (deadline_ms > now) ? (deadline_ms - now) / 1000 : 0;
    }
    snprintf(sdl_status_json, sizeof(sdl_status_json),
             "{\"active\":%s,\"file\":\"%s\","
             "\"elapsed_s\":%lu,\"remaining_s\":%lu,"
             "\"lines\":%lu,\"bytes\":%lu,\"buf_used\":%u}",
             sdl_active ? "true" : "false",
             sdl_active ? sdl_filepath : "",
             (unsigned long)elapsed_s,
             (unsigned long)remaining_s,
             (unsigned long)sdl_lines_written,
             (unsigned long)sdl_bytes_written,
             (unsigned)sdl_buf_len);
    return sdl_status_json;
}
