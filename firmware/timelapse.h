#pragma once
#include <Arduino.h>

#ifdef INCLUDE_TIMELAPSE

// Start the time-lapse task (runs on Core 0, low priority)
void startTimelapseTask();

// Enable/disable at runtime (also settable via /settings POST)
void setTimelapseEnabled(bool enabled);
bool getTimelapseEnabled();

// Helper: get per-day directory path on SD (creates if needed)
// Returns path like "/20260409" (root-relative — Arduino SD mounts at "/")
String getPerDayDir();

#else

static inline void startTimelapseTask() {}
static inline void setTimelapseEnabled(bool) {}
static inline bool getTimelapseEnabled() { return false; }
static inline String getPerDayDir() { return "/"; }

#endif
