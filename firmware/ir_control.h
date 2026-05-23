#ifndef IR_CONTROL_H
#define IR_CONTROL_H

#include <Arduino.h>
#include <Wire.h>
#include <freertos/semphr.h>

// I2C mutex to serialize LTR308 reads with camera SCCB bus
// Camera driver uses SCCB internally (can't wrap), but this prevents
// LTR308 reads from colliding with each other across tasks.
// LTR308 reads happen every ~5s and take <5ms, so contention is low.
extern SemaphoreHandle_t i2c_mutex;

// GPIO Pins
#define IR_LED_PIN     GPIO_NUM_47

// LTR-308 Ambient Light Sensor I2C Configuration
#define LTR308_I2C_ADDR     0x53
#define LTR308_SDA_PIN      GPIO_NUM_8   // Shared with camera SIOD
#define LTR308_SCL_PIN      GPIO_NUM_9   // Shared with camera SIOC

// LTR-308 Registers
#define LTR308_MAIN_CTRL    0x00
#define LTR308_ALS_MEAS     0x04
#define LTR308_ALS_GAIN     0x05
#define LTR308_PART_ID      0x06
#define LTR308_MAIN_STATUS  0x07
#define LTR308_ALS_DATA_0   0x0D
#define LTR308_ALS_DATA_1   0x0E
#define LTR308_ALS_DATA_2   0x0F

// Default configuration
#define DEFAULT_LUX_THRESHOLD    13.0f     // Lux threshold for auto mode
#define DEFAULT_AUTO_MODE        true      // Automatic mode enabled
#define MIN_LUX_THRESHOLD        0.1f
#define MAX_LUX_THRESHOLD        1000.0f

// IR Control Configuration Structure
struct IRConfig {
    bool auto_mode;              // Automatic IR LED control based on ambient light
    float lux_threshold;         // Lux value to trigger IR LED
    bool manual_state;           // Manual ON/OFF state (when auto_mode = false)
    bool time_based;             // Use time-based control
    int night_start_hour;        // Night mode start (24h format)
    int night_end_hour;          // Night mode end (24h format)
    bool ir_led_current_state;   // Current IR LED state
    float last_lux_reading;      // Last ambient light reading
    unsigned long last_update;   // Last update timestamp
};

// Function declarations
void initIRControl();
bool readAmbientLight(float* lux);        // Returns cached lux (safe from any context)
bool readAmbientLightSCCBSafe();           // Actual I2C read (call ONLY from captureTask after fb_return)
void setIRLED(bool state);
void updateIRAutoMode();
IRConfig getIRConfig();
void setIRConfig(const IRConfig& config);
bool saveIRConfig();
bool loadIRConfig();
String getIRStatusJSON();

// LTR-308 specific functions
bool initLTR308();
bool readLTR308Raw(uint32_t* rawValue);
float convertToLux(uint32_t rawValue);

#endif // IR_CONTROL_H
