#include "zone_manager.h"
#include "ArduinoJson.h"
#include <LittleFS.h>

#define ZONES_FILE    "/zones.json"
#define ROI_MASK_FILE "/roi_mask.txt"

static Zone zones[ZONE_MAX_COUNT];
static int zone_count = 0;

static bool loadZones() {
    if (!LittleFS.exists(ZONES_FILE)) return false;
    File f = LittleFS.open(ZONES_FILE, "r");
    if (!f) return false;

    DynamicJsonDocument doc(2048);
    if (deserializeJson(doc, f) != DeserializationError::Ok) { f.close(); return false; }
    f.close();

    JsonArray arr = doc.as<JsonArray>();
    zone_count = 0;
    for (JsonObject obj : arr) {
        if (zone_count >= ZONE_MAX_COUNT) break;
        Zone& z = zones[zone_count];
        strlcpy(z.name,  obj["name"]  | "", ZONE_NAME_LEN);
        strlcpy(z.color, obj["color"] | "#ffffff", 8);
        z.alert = obj["alert"] | false;
        z.rect_count = 0;
        JsonArray rects = obj["rects"];
        if (rects) {
            for (JsonObject r : rects) {
                if (z.rect_count >= ZONE_MAX_RECTS) break;
                ZoneRect& zr = z.rects[z.rect_count++];
                zr.x = r["x"] | 0;
                zr.y = r["y"] | 0;
                zr.w = r["w"] | 1;
                zr.h = r["h"] | 1;
            }
        }
        zone_count++;
    }
    return true;
}

bool saveZones() {
    DynamicJsonDocument doc(2048);
    JsonArray arr = doc.to<JsonArray>();
    for (int i = 0; i < zone_count; i++) {
        Zone& z = zones[i];
        JsonObject obj = arr.createNestedObject();
        obj["name"]  = z.name;
        obj["color"] = z.color;
        obj["alert"] = z.alert;
        JsonArray rects = obj.createNestedArray("rects");
        for (int r = 0; r < z.rect_count; r++) {
            JsonObject ro = rects.createNestedObject();
            ro["x"] = z.rects[r].x;
            ro["y"] = z.rects[r].y;
            ro["w"] = z.rects[r].w;
            ro["h"] = z.rects[r].h;
        }
    }
    File f = LittleFS.open(ZONES_FILE, "w");
    if (!f) return false;
    serializeJson(doc, f);
    f.close();
    return true;
}

void initZoneManager() {
    loadZones();
    Serial.printf("ZoneManager: %d zone(s) loaded\n", zone_count);
}

int   getZoneCount()       { return zone_count; }
Zone* getZone(int index)   { return (index >= 0 && index < zone_count) ? &zones[index] : nullptr; }

bool addOrUpdateZone(const Zone& z) {
    // Update if name exists
    for (int i = 0; i < zone_count; i++) {
        if (strncmp(zones[i].name, z.name, ZONE_NAME_LEN) == 0) {
            zones[i] = z;
            return saveZones();
        }
    }
    if (zone_count >= ZONE_MAX_COUNT) return false;
    zones[zone_count++] = z;
    return saveZones();
}

bool deleteZone(const char* name) {
    for (int i = 0; i < zone_count; i++) {
        if (strncmp(zones[i].name, name, ZONE_NAME_LEN) == 0) {
            for (int j = i; j < zone_count - 1; j++) zones[j] = zones[j + 1];
            zone_count--;
            return saveZones();
        }
    }
    return false;
}

void clearZones() {
    zone_count = 0;
    saveZones();
}

void getActiveZones(const bool* motion_grid, int gw, int gh,
                    char* out_buf, int buf_len) {
    out_buf[0] = '\0';
    int pos = 0;
    for (int i = 0; i < zone_count; i++) {
        Zone& z = zones[i];
        if (!z.alert) continue;
        bool hit = false;
        for (int r = 0; r < z.rect_count && !hit; r++) {
            ZoneRect& zr = z.rects[r];
            for (int y = zr.y; y < zr.y + zr.h && !hit; y++) {
                for (int x = zr.x; x < zr.x + zr.w && !hit; x++) {
                    if (x >= 0 && x < gw && y >= 0 && y < gh) {
                        if (motion_grid[y * gw + x]) hit = true;
                    }
                }
            }
        }
        if (hit) {
            if (pos > 0 && pos < buf_len - 2) out_buf[pos++] = ',';
            int remaining = buf_len - pos - 1;
            int len = strnlen(z.name, ZONE_NAME_LEN);
            if (len > remaining) len = remaining;
            memcpy(out_buf + pos, z.name, len);
            pos += len;
            out_buf[pos] = '\0';
        }
    }
}

String getZonesJSON() {
    DynamicJsonDocument doc(2048);
    JsonArray arr = doc.to<JsonArray>();
    for (int i = 0; i < zone_count; i++) {
        Zone& z = zones[i];
        JsonObject obj = arr.createNestedObject();
        obj["name"]  = z.name;
        obj["color"] = z.color;
        obj["alert"] = z.alert;
        JsonArray rects = obj.createNestedArray("rects");
        for (int r = 0; r < z.rect_count; r++) {
            JsonObject ro = rects.createNestedObject();
            ro["x"] = z.rects[r].x;
            ro["y"] = z.rects[r].y;
            ro["w"] = z.rects[r].w;
            ro["h"] = z.rects[r].h;
        }
    }
    String out;
    serializeJson(doc, out);
    return out;
}

void saveROIMask(const char* mask_str, int len) {
    File f = LittleFS.open(ROI_MASK_FILE, "w");
    if (!f) return;
    f.write((const uint8_t*)mask_str, len);
    f.close();
}

bool loadROIMask(char* out_buf, int buf_len) {
    if (!LittleFS.exists(ROI_MASK_FILE)) return false;
    File f = LittleFS.open(ROI_MASK_FILE, "r");
    if (!f) return false;
    int bytes = f.readBytes(out_buf, buf_len - 1);
    out_buf[bytes] = '\0';
    f.close();
    return bytes > 0;
}
