#include "ws_log.h"
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <stdarg.h>
#include <stdio.h>

// Ring buffer for log lines
static char logBuffer[WS_LOG_LINES][WS_LOG_LINE_LEN];
static int logHead = 0;       // Next write position
static int logCount = 0;      // Total lines stored (max WS_LOG_LINES)
static SemaphoreHandle_t logMutex = NULL;

void wsLogInit() {
    logMutex = xSemaphoreCreateMutex();
    memset(logBuffer, 0, sizeof(logBuffer));
    logHead = 0;
    logCount = 0;
}

void wsLog(const char* fmt, ...) {
    char line[WS_LOG_LINE_LEN];
    va_list args;
    va_start(args, fmt);
    vsnprintf(line, sizeof(line), fmt, args);
    va_end(args);

    // Always print to Serial
    Serial.println(line);

    // Store in ring buffer
    if (logMutex && xSemaphoreTake(logMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
        strncpy(logBuffer[logHead], line, WS_LOG_LINE_LEN - 1);
        logBuffer[logHead][WS_LOG_LINE_LEN - 1] = '\0';
        logHead = (logHead + 1) % WS_LOG_LINES;
        if (logCount < WS_LOG_LINES) logCount++;
        xSemaphoreGive(logMutex);
    }
}

String wsLogGetBuffer() {
    String result;
    result.reserve(WS_LOG_LINES * 80);

    if (logMutex && xSemaphoreTake(logMutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        int start = (logCount < WS_LOG_LINES) ? 0 : logHead;
        for (int i = 0; i < logCount; i++) {
            int idx = (start + i) % WS_LOG_LINES;
            result += logBuffer[idx];
            result += '\n';
        }
        xSemaphoreGive(logMutex);
    }
    return result;
}

// HTTP handler: GET /log — returns recent log as plain text
static esp_err_t log_handler(httpd_req_t *req) {
    httpd_resp_set_type(req, "text/plain; charset=utf-8");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    String buf = wsLogGetBuffer();
    httpd_resp_send(req, buf.c_str(), buf.length());
    return ESP_OK;
}

// HTTP handler: GET /log-viewer — minimal HTML page with auto-refresh log
static const char LOG_VIEWER_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>ESP32 Live Log</title>
<style>
body{background:#1a1a2e;color:#0f0;font:13px/1.4 monospace;margin:0;padding:10px}
#log{white-space:pre-wrap;word-break:break-all;height:calc(100vh - 60px);overflow-y:auto}
h2{color:#e94560;margin:0 0 8px}
.controls{margin-bottom:8px}
.controls button{background:#16213e;color:#fff;border:1px solid #0f3460;padding:4px 12px;cursor:pointer;margin-right:5px}
.controls button:hover{background:#0f3460}
</style></head><body>
<h2>ESP32 Live Log</h2>
<div class="controls">
<button onclick="toggleScroll()">Auto-scroll: ON</button>
<button onclick="clearLog()">Clear</button>
<button onclick="downloadLog()">Download</button>
</div>
<div id="log"></div>
<script>
let autoScroll=true,logEl=document.getElementById('log');
function toggleScroll(){autoScroll=!autoScroll;event.target.textContent='Auto-scroll: '+(autoScroll?'ON':'OFF')}
function clearLog(){logEl.textContent=''}
function downloadLog(){let a=document.createElement('a');a.href='data:text/plain,'+encodeURIComponent(logEl.textContent);a.download='esp32_log.txt';a.click()}
async function fetchLog(){try{let r=await fetch('/log');if(r.ok){let t=await r.text();if(t!==logEl.textContent){logEl.textContent=t;if(autoScroll)logEl.scrollTop=logEl.scrollHeight}}}catch(e){}}
setInterval(fetchLog,2000);fetchLog();
</script></body></html>
)rawliteral";

static esp_err_t log_viewer_handler(httpd_req_t *req) {
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_send(req, LOG_VIEWER_HTML, strlen(LOG_VIEWER_HTML));
    return ESP_OK;
}

void wsLogRegisterHandlers(httpd_handle_t server) {
    httpd_uri_t log_uri = {
        .uri = "/log",
        .method = HTTP_GET,
        .handler = log_handler,
        .user_ctx = NULL
    };
    httpd_uri_t log_viewer_uri = {
        .uri = "/log-viewer",
        .method = HTTP_GET,
        .handler = log_viewer_handler,
        .user_ctx = NULL
    };
    httpd_register_uri_handler(server, &log_uri);
    httpd_register_uri_handler(server, &log_viewer_uri);

    Serial.println("Live log endpoints: /log (text), /log-viewer (HTML)");
}
