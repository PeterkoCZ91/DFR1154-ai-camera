#ifndef ZONE_MANAGER_H
#define ZONE_MANAGER_H

#include <Arduino.h>

#define ZONE_MAX_COUNT   8
#define ZONE_MAX_RECTS   4
#define ZONE_NAME_LEN   24

struct ZoneRect {
    int x, y, w, h;   // grid block coordinates
};

struct Zone {
    char name[ZONE_NAME_LEN];
    char color[8];     // "#rrggbb"
    bool alert;        // send Telegram/event when motion in this zone
    int  rect_count;
    ZoneRect rects[ZONE_MAX_RECTS];
};

void     initZoneManager();
bool     saveZones();

int      getZoneCount();
Zone*    getZone(int index);
bool     addOrUpdateZone(const Zone& z);
bool     deleteZone(const char* name);
void     clearZones();

// Returns comma-separated names of zones containing motion at block (x,y)
// Returns "" if none. Caller provides buffer.
void     getActiveZones(const bool* motion_grid, int gw, int gh,
                        char* out_buf, int buf_len);

String   getZonesJSON();

// ROI mask persistence
void     saveROIMask(const char* mask_str, int len);
bool     loadROIMask(char* out_buf, int buf_len);

#endif // ZONE_MANAGER_H
