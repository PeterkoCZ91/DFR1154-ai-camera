#pragma once
#include <Arduino.h>
#include "esp_http_server.h"

// SD debug logger — session-based, batched writes, minimal SD wear.
//
// Normal operation: zero overhead, nothing written.
// Debug session: wsLog() lines buffered in RAM (4KB), flushed to SD every 60s.
// Auto-stops after timeout to prevent forgetting.
//
// API:  POST /sd-log  {"action":"start","timeout_h":4}
//       POST /sd-log  {"action":"stop"}
//       POST /sd-log  {"action":"flush"}
//       GET  /sd-log  → session status JSON
// GUI:  SD Log toggle in dashboard

#define SDL_BUF_SIZE    4096   // RAM write buffer (bytes)
#define SDL_FLUSH_MS    60000  // Flush to SD every N ms
#define SDL_MAX_AGE_DAYS   30  // Delete log files older than N days
#define SDL_MAX_DIR_MB     50  // Max total size of /sdcard/logs/ (MB)
#define SDL_DEFAULT_TIMEOUT_H  4  // Auto-stop after N hours (0 = no timeout)

// Must be called after SD is mounted. Registers /sd-log endpoint.
void sdLogInit(httpd_handle_t server);

// Start a debug session. timeout_h=0 means no auto-stop.
bool sdLogStart(uint32_t timeout_h = SDL_DEFAULT_TIMEOUT_H);

// Stop current session and flush remaining buffer.
void sdLogStop();

// Flush RAM buffer to SD without stopping.
void sdLogFlush();

// Write a line to the buffer (called from wsLog when session active).
// Thread-safe. No-op when session is not active.
void sdLogWrite(const char* line);

// Periodic maintenance — call from a slow task (every few seconds).
void sdLogTick();

// Returns true if a session is currently active.
bool sdLogIsActive();

// Returns status JSON string (static buffer, valid until next call).
const char* sdLogStatusJson();
