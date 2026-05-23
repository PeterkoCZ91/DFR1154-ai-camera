#ifndef TASK_PRIORITIES_H
#define TASK_PRIORITIES_H

// ========================================
// FreeRTOS Task Priority Definitions
// ========================================
// Lower number = lower priority
// Range: 0 (lowest) to configMAX_PRIORITIES (highest, typically 25)

// CRITICAL - Camera capture & time-sensitive operations
#define PRIORITY_CRITICAL     10

// HIGH - Real-time streaming & network
#define PRIORITY_HIGH         8

// NORMAL - HTTP handlers, web UI
#define PRIORITY_NORMAL       5

// LOW - Background tasks (logging, monitoring)
#define PRIORITY_LOW          2

// IDLE - Cleanup, maintenance
#define PRIORITY_IDLE         1

#endif // TASK_PRIORITIES_H
