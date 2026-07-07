#include "event_log.h"
#include "ArduinoJson.h"
#include <LittleFS.h>

#define EVENTS_FILE "/events.jsonl"

static EventEntry ring[EVENT_LOG_SIZE];
static int ring_head = 0;
static int ring_count = 0;
static portMUX_TYPE ring_mux = portMUX_INITIALIZER_UNLOCKED;

const char* eventTypeName(EventType type) {
    switch (type) {
        case EVT_BOOT:               return "boot";
        case EVT_MOTION:             return "motion";
        case EVT_PERSON:             return "person";
        case EVT_ARMED:              return "armed";
        case EVT_DISARMED:           return "disarmed";
        case EVT_RECORDING_STARTED:  return "recording_started";
        case EVT_RECORDING_STOPPED:  return "recording_stopped";
        case EVT_TELEGRAM_FAILED:    return "telegram_failed";
        case EVT_TELEGRAM_SENT:      return "telegram_sent";
        case EVT_TAMPER:             return "tamper";
        case EVT_WIFI_RECONNECT:     return "wifi_reconnect";
        case EVT_LOW_MEMORY:         return "low_memory";
        case EVT_SD_FAILURE:         return "sd_failure";
        default:                     return "unknown";
    }
}

static void appendToFile(const EventEntry& e) {
    File f = LittleFS.open(EVENTS_FILE, "a");
    if (!f) return;

    // One JSON object per line: {"u":12345,"t":"motion","d":"score=42"}
    f.printf("{\"u\":%lu,\"t\":\"%s\",\"d\":\"%s\"}\n",
             (unsigned long)e.uptime_s, eventTypeName(e.type), e.detail);
    f.close();

    // Rotate: if file exceeds EVENT_LOG_SIZE lines rewrite with tail
    File r = LittleFS.open(EVENTS_FILE, "r");
    if (!r) return;
    int lineCount = 0;
    while (r.available()) { if (r.read() == '\n') lineCount++; }
    r.close();

    if (lineCount <= EVENT_LOG_SIZE) return;

    // Keep last EVENT_LOG_SIZE lines
    r = LittleFS.open(EVENTS_FILE, "r");
    if (!r) return;
    // Skip first (lineCount - EVENT_LOG_SIZE) lines
    int skip = lineCount - EVENT_LOG_SIZE;
    for (int i = 0; i < skip && r.available(); i++) {
        while (r.available() && r.read() != '\n') {}
    }
    String tail = r.readString();
    r.close();

    File w = LittleFS.open(EVENTS_FILE, "w");
    if (w) { w.print(tail); w.close(); }
}

static void loadFromFile() {
    if (!LittleFS.exists(EVENTS_FILE)) return;
    File f = LittleFS.open(EVENTS_FILE, "r");
    if (!f) return;

    while (f.available()) {
        String line = f.readStringUntil('\n');
        line.trim();
        if (line.length() == 0) continue;

        StaticJsonDocument<128> doc;
        if (deserializeJson(doc, line) != DeserializationError::Ok) continue;

        EventEntry e;
        e.uptime_s = doc["u"] | 0;
        e.type = EVT_UNKNOWN;
        const char* tname = doc["t"] | "";
        for (int i = EVT_BOOT; i <= EVT_UNKNOWN; i++) {
            if (strcmp(eventTypeName((EventType)i), tname) == 0) {
                e.type = (EventType)i; break;
            }
        }
        strlcpy(e.detail, doc["d"] | "", EVENT_DETAIL_LEN);

        ring[ring_head] = e;
        ring_head = (ring_head + 1) % EVENT_LOG_SIZE;
        if (ring_count < EVENT_LOG_SIZE) ring_count++;
    }
    f.close();
}

void initEventLog(const char* boot_detail) {
    memset(ring, 0, sizeof(ring));
    loadFromFile();
    logEvent(EVT_BOOT, boot_detail);
}

void logEvent(EventType type, const char* detail) {
    EventEntry e;
    e.uptime_s = millis() / 1000;
    e.type = type;
    strlcpy(e.detail, detail ? detail : "", EVENT_DETAIL_LEN);

    portENTER_CRITICAL(&ring_mux);
    ring[ring_head] = e;
    ring_head = (ring_head + 1) % EVENT_LOG_SIZE;
    if (ring_count < EVENT_LOG_SIZE) ring_count++;
    portEXIT_CRITICAL(&ring_mux);

    appendToFile(e);
    Serial.printf("[EVT] %s %s\n", eventTypeName(type), detail ? detail : "");
}

String getEventsJSON() {
    // Output newest-first, up to ring_count entries
    String out = "[";
    portENTER_CRITICAL(&ring_mux);
    int count = ring_count;
    int head = ring_head;
    portEXIT_CRITICAL(&ring_mux);

    for (int i = 0; i < count; i++) {
        int idx = (head - 1 - i + EVENT_LOG_SIZE) % EVENT_LOG_SIZE;
        const EventEntry& e = ring[idx];
        if (i > 0) out += ",";
        out += "{\"uptime_s\":";
        out += e.uptime_s;
        out += ",\"type\":\"";
        out += eventTypeName(e.type);
        out += "\",\"detail\":\"";
        out += e.detail;
        out += "\"}";
    }
    out += "]";
    return out;
}
