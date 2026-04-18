#pragma once
#include <Arduino.h>
#include "esp_http_server.h"

// --- Live Log System ---
// Ring buffer stores last N log lines.
// /log       — plain text (last 100 lines)
// /log-viewer — HTML page with auto-refresh (2s polling)
// Usage: wsLog("message %d", value) — prints to Serial + stores in buffer

#define WS_LOG_LINES 100         // Number of lines to keep in ring buffer
#define WS_LOG_LINE_LEN 200     // Max chars per line

// Initialize the log system (call before wsLogRegisterHandlers)
void wsLogInit();

// Add a log line (thread-safe, also prints to Serial)
void wsLog(const char* fmt, ...) __attribute__((format(printf, 1, 2)));

// Register HTTP handlers on the given server
void wsLogRegisterHandlers(httpd_handle_t server);

// Get the full log buffer as a single string
String wsLogGetBuffer();
