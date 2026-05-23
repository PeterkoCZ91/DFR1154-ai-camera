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
    EVT_UNKNOWN
};

struct EventEntry {
    uint32_t uptime_s;
    EventType type;
    char detail[EVENT_DETAIL_LEN];
};

void initEventLog();
void logEvent(EventType type, const char* detail = "");
String getEventsJSON();
const char* eventTypeName(EventType type);

#endif // EVENT_LOG_H
