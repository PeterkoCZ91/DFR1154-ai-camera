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
    unsigned long longest_uptime;
};

// Global instances (defined in main.cpp)
extern WiFiHealth wifi_health;
extern SystemStats sys_stats;

// Helper function declarations
const char* getWiFiQuality(int rssi);

#endif
