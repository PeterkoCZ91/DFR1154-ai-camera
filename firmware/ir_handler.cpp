#include "ir_control.h"
#include "config.h"
#include "board_config.h"
#include <LittleFS.h>
#include <ArduinoJson.h>
#include <Wire.h>

// LTR-308 Registers
#define LTR308_ADDR         0x53
#define LTR308_MAIN_CTRL    0x00
#define LTR308_MEAS_RATE    0x04
#define LTR308_GAIN         0x05
#define LTR308_PART_ID      0x06
#define LTR308_STATUS       0x07
#define LTR308_DATA_0       0x0D
#define LTR308_DATA_1       0x0E
#define LTR308_DATA_2       0x0F

// I2C mutex (global, shared with camera SCCB bus)
SemaphoreHandle_t i2c_mutex = NULL;

// Global IR configuration
static IRConfig irConfig = {
    .auto_mode = DEFAULT_AUTO_MODE,
    .lux_threshold = DEFAULT_LUX_THRESHOLD,
    .manual_state = false,
    .time_based = false,
    .night_start_hour = 20,
    .night_end_hour = 7,
    .ir_led_current_state = false,
    .last_lux_reading = 0.0f,
    .last_update = 0
};

static bool ltr308_initialized = false;

// Forward declarations
bool initLTR308();
bool readLTR308Raw(uint32_t* rawValue);
float convertToLux(uint32_t rawValue);

/**
 * Initialize IR control subsystem
 */
void initIRControl() {
    Serial.println("Initializing IR Control...");

    // Create I2C mutex (protects LTR308 Wire transactions from concurrent access)
    if (i2c_mutex == NULL) {
        i2c_mutex = xSemaphoreCreateMutex();
        if (i2c_mutex) {
            Serial.println("✅ I2C mutex created");
        } else {
            Serial.println("❌ Failed to create I2C mutex!");
        }
    }

    // Configure IR LED pin
    pinMode(IR_LED_PIN, OUTPUT);
    digitalWrite(IR_LED_PIN, LOW);
    Serial.println("IR LED configured");

    // Initialize I2C
    Wire.begin(I2C_SDA, I2C_SCL);
    
    // Initialize Sensor manually
    if (initLTR308()) {
        Serial.println("✅ LTR-308 Ambient Light Sensor initialized!");
        ltr308_initialized = true;
    } else {
        Serial.println("⚠️ LTR-308 initialization failed! (Check I2C pins)");
        ltr308_initialized = false;
    }

    // Load configuration
    if (loadIRConfig()) {
        Serial.println("IR configuration loaded");
    } else {
        Serial.println("Using default IR configuration");
        saveIRConfig();
    }

    // Apply initial state
    if (irConfig.auto_mode) {
        updateIRAutoMode();
    } else {
        setIRLED(irConfig.manual_state);
    }
}

/**
 * Initialize LTR-308 (Direct I2C)
 * Must be called after i2c_mutex is created.
 */
bool initLTR308() {
    if (i2c_mutex && xSemaphoreTake(i2c_mutex, pdMS_TO_TICKS(50)) != pdTRUE) {
        Serial.println("⚠️ initLTR308: I2C mutex timeout");
        return false;
    }

    bool success = false;

    // 1. Check Part ID
    Wire.beginTransmission(LTR308_ADDR);
    Wire.write(LTR308_PART_ID);
    if (Wire.endTransmission() != 0) goto done;

    Wire.requestFrom(LTR308_ADDR, 1);
    if (Wire.available()) {
        uint8_t id = Wire.read();
        if (id != 0xB1) {
            Serial.printf("⚠️ Unknown Sensor ID: 0x%02X (Expected 0xB1)\n", id);
            // Relaxed check, might be compatible
        }
    } else {
        goto done;
    }

    // 2. Main Control: Active Mode (Bit 1 = 1), Reset (Bit 4 = 0)
    Wire.beginTransmission(LTR308_ADDR);
    Wire.write(LTR308_MAIN_CTRL);
    Wire.write(0x02); // Active Mode
    Wire.endTransmission();

    delay(10);

    // 3. Measurement Rate: 100ms integration (16-bit), 200ms rate
    // Was: 0x03 = 400ms/500ms (20-bit) — too slow for sunset/headlight reactions
    // Now: 0x21 = bits[5:3]=100 (100ms 17-bit) | bits[2:0]=001 (100ms rate)
    // Trade-off: 17-bit resolution still gives 0.1 lux precision (sufficient)
    Wire.beginTransmission(LTR308_ADDR);
    Wire.write(LTR308_MEAS_RATE);
    Wire.write(0x21); // 100ms integration / 100ms rate (faster reaction to light changes)
    Wire.endTransmission();

    // 4. Gain: 18x (maximum sensitivity for indoor use)
    Wire.beginTransmission(LTR308_ADDR);
    Wire.write(LTR308_GAIN);
    Wire.write(0x04); // 0x00=1x, 0x01=3x, 0x02=6x, 0x03=9x, 0x04=18x
    Wire.endTransmission();

    success = true;

done:
    if (i2c_mutex) xSemaphoreGive(i2c_mutex);
    return success;
}

/**
 * Read Ambient Light (returns cached value)
 *
 * LTR-308 shares I2C bus (GPIO 8/9) with camera SCCB. The camera driver
 * holds the bus during frame capture, so Wire reads from loop() context
 * always fail. Actual I2C reads happen in captureTask via
 * readAmbientLightSCCBSafe() when the bus is free after fb_return().
 */
bool readAmbientLight(float* lux) {
    if (!ltr308_initialized) {
        *lux = -1.0f;
        return false;
    }
    *lux = irConfig.last_lux_reading;
    return true;
}

/**
 * Read LTR-308 from captureTask context (SCCB bus free after fb_return)
 * No mutex needed — called exclusively from captureTask when bus is idle.
 * Updates irConfig.last_lux_reading for all consumers.
 */
bool readAmbientLightSCCBSafe() {
    if (!ltr308_initialized) return false;

    // Read Status register
    Wire.beginTransmission(LTR308_ADDR);
    Wire.write(LTR308_STATUS);
    if (Wire.endTransmission() != 0) return false;

    Wire.requestFrom(LTR308_ADDR, 1);
    if (!Wire.available()) return false;

    uint8_t status = Wire.read();
    if (!(status & 0x08)) return true; // No new data, cached value still valid

    // Read Data (3 bytes: LSB, MID, MSB)
    Wire.beginTransmission(LTR308_ADDR);
    Wire.write(LTR308_DATA_0);
    if (Wire.endTransmission() != 0) return false;

    Wire.requestFrom(LTR308_ADDR, 3);
    if (Wire.available() < 3) return false;

    uint32_t data0 = Wire.read();
    uint32_t data1 = Wire.read();
    uint32_t data2 = Wire.read();

    uint32_t raw = (data2 << 16) | (data1 << 8) | data0;
    raw &= 0x01FFFF; // 17-bit resolution (was 20-bit @ 400ms integration)

    // Convert to Lux: 0.6 * raw / (gain=18 * int_factor=1.0)
    // int_factor: 100ms→1.0 (was 400ms→4.0)
    float lux = raw * 0.0333f;
    irConfig.last_lux_reading = lux;
    Serial.printf("💡 LTR-308: raw=%lu lux=%.1f\n", raw, lux);
    return true;
}

/**
 * Set IR LED state (ON/OFF)
 */
void setIRLED(bool state) {
    digitalWrite(IR_LED_PIN, state ? HIGH : LOW);
    irConfig.ir_led_current_state = state;
    irConfig.last_update = millis();
    Serial.printf("IR LED: %s\n", state ? "ON" : "OFF");
}

/**
 * Update IR LED state based on automatic mode logic
 */
void updateIRAutoMode() {
    if (!irConfig.auto_mode) return;

    bool shouldBeOn = false;

    // 1. Time-based override
    if (irConfig.time_based) {
        struct tm timeinfo;
        if (getLocalTime(&timeinfo)) {
            int currentHour = timeinfo.tm_hour;
            if (irConfig.night_start_hour > irConfig.night_end_hour) {
                shouldBeOn = (currentHour >= irConfig.night_start_hour || currentHour < irConfig.night_end_hour);
            } else {
                shouldBeOn = (currentHour >= irConfig.night_start_hour && currentHour < irConfig.night_end_hour);
            }
            
            if (shouldBeOn != irConfig.ir_led_current_state) {
                setIRLED(shouldBeOn);
                Serial.printf("IR Time Mode: %s\n", shouldBeOn ? "ON" : "OFF");
            }
            return;
        }
    }

    // 2. Sensor-based control
    float lux = 0.0f;
    if (readAmbientLight(&lux)) {
        // Hysteresis logic
        float threshold = irConfig.lux_threshold;
        float hysteresis = max(1.0f, threshold * 0.20f); // 20% hysteresis or min 1 lux

        if (irConfig.ir_led_current_state) {
            // Currently ON -> turn OFF if brighter
            if (lux > (threshold + hysteresis)) shouldBeOn = false;
            else shouldBeOn = true;
        } else {
            // Currently OFF -> turn ON if darker
            if (lux < (threshold - hysteresis)) shouldBeOn = true;
            else shouldBeOn = false;
        }

        if (shouldBeOn != irConfig.ir_led_current_state) {
            setIRLED(shouldBeOn);
            Serial.printf("IR Lux Mode: %.2f lux -> %s\n", lux, shouldBeOn ? "ON" : "OFF");
        }
    }
}

IRConfig getIRConfig() { return irConfig; }

void setIRConfig(const IRConfig& config) {
    irConfig = config;
    if (irConfig.auto_mode) updateIRAutoMode();
    else setIRLED(irConfig.manual_state);
    saveIRConfig();
}

bool saveIRConfig() {
    StaticJsonDocument<256> doc;
    doc["auto_mode"] = irConfig.auto_mode;
    doc["lux_threshold"] = irConfig.lux_threshold;
    doc["manual_state"] = irConfig.manual_state;
    doc["time_based"] = irConfig.time_based;
    doc["night_start_hour"] = irConfig.night_start_hour;
    doc["night_end_hour"] = irConfig.night_end_hour;

    File file = LittleFS.open("/ir_config.json", "w");
    if (!file) return false;
    serializeJson(doc, file);
    file.close();
    return true;
}

bool loadIRConfig() {
    if (!LittleFS.exists("/ir_config.json")) return false;
    File file = LittleFS.open("/ir_config.json", "r");
    if (!file) return false;

    StaticJsonDocument<256> doc;
    DeserializationError error = deserializeJson(doc, file);
    file.close();

    if (error) return false;

    irConfig.auto_mode = doc["auto_mode"] | DEFAULT_AUTO_MODE;
    irConfig.lux_threshold = doc["lux_threshold"] | DEFAULT_LUX_THRESHOLD;
    irConfig.manual_state = doc["manual_state"] | false;
    irConfig.time_based = doc["time_based"] | false;
    irConfig.night_start_hour = doc["night_start_hour"] | 20;
    irConfig.night_end_hour = doc["night_end_hour"] | 7;
    return true;
}

String getIRStatusJSON() {
    StaticJsonDocument<384> doc;
    doc["ir_led_state"] = irConfig.ir_led_current_state;
    doc["auto_mode"] = irConfig.auto_mode;
    doc["lux_threshold"] = irConfig.lux_threshold;
    doc["manual_state"] = irConfig.manual_state;
    doc["ambient_light_lux"] = irConfig.last_lux_reading;
    doc["ltr308_available"] = ltr308_initialized;
    doc["time_based"] = irConfig.time_based;
    doc["night_start_hour"] = irConfig.night_start_hour;
    doc["night_end_hour"] = irConfig.night_end_hour;
    doc["last_update_ms"] = irConfig.last_update;

    String output;
    serializeJson(doc, output);
    return output;
}