#ifndef EVENT_LOG_H
#define EVENT_LOG_H

#include <Arduino.h>

#define EVENT_LOG_SIZE    100
#define EVENT_DETAIL_LEN   48

enum EventType {
    EVT_BOOT = 0,
    EVT_MOTION,
    EVT_PERSON,
    EVT_ARMED,
    EVT_DISARMED,
    EVT_RECORDING_STARTED,
    EVT_RECORDING_STOPPED,
    EVT_TELEGRAM_FAILED,
    EVT_TELEGRAM_SENT,
    EVT_TAMPER,
    EVT_WIFI_RECONNECT,
    EVT_LOW_MEMORY,
    EVT_SD_FAILURE,
    EVT_RESTART,
    EVT_UNKNOWN
};

struct EventEntry {
    uint32_t uptime_s;
    EventType type;
    char detail[EVENT_DETAIL_LEN];
};

void initEventLog(const char* boot_detail = "");
void logEvent(EventType type, const char* detail = "");
String getEventsJSON();

// Record why the firmware is restarting, then restart. The reset-reason code
// the next boot reports only separates SW from PANIC — it can never say which
// of the firmware's own restart paths ran, and every one of them used to leave
// the log silent. Use this instead of ESP.restart() so the reboot log answers
// "why", not just "which kind". Never returns.
void restartWithReason(const char* reason);
const char* eventTypeName(EventType type);

#endif // EVENT_LOG_H
