#ifndef HEALTH_MONITOR_H
#define HEALTH_MONITOR_H

#include <Arduino.h>

// Structure to track WiFi health metrics
struct WiFiHealth {
    int rssi;
    int reconnect_count;
    unsigned long last_reconnect_time;
    bool signal_degraded;
    char quality[16]; // "excellent", "good", "fair", "poor", "critical" -- fixed buffer, thread-safe
};

// Structure to track system stability statistics (persisted in flash)
struct SystemStats {
    uint32_t total_restarts;
    uint32_t last_restart_reason;
    uint32_t poweron_restarts;
    uint32_t brownout_restarts;
    uint32_t wdt_restarts;
    uint32_t other_restarts;
    unsigned long longest_uptime;
};

// Global instances (defined in main.cpp)
extern WiFiHealth wifi_health;
extern SystemStats sys_stats;

// Helper function declarations
const char* getWiFiQuality(int rssi);
const char* getResetReasonName(uint32_t reason);

// Restart-rate detection (boot-timestamp ring buffer persisted in LittleFS).
// Returns how many recorded boots fall within the last `window_seconds`.
// Returns 0 when wall-clock time is not yet available (NTP unsynced), so callers
// must treat 0 as "unknown", not "healthy". See main.cpp for the implementation.
uint8_t getRestartsInWindow(uint32_t window_seconds);
bool bootTimeRecorded();

#endif
