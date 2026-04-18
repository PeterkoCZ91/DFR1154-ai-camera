#ifndef BOARD_CONFIG_H
#define BOARD_CONFIG_H

// DFRobot FireBeetle2 ESP32-S3 with AI Camera
#define CAMERA_MODEL_DFRobot_FireBeetle2_ESP32S3

#include "camera_pins.h"

// --- Hardware Pin Definitions ---

// PDM Microphone (I2S RX)
#define MIC_I2S_CLK     38
#define MIC_I2S_DATA    39

// Speaker / Amplifier (I2S TX)
#define SPK_I2S_BCLK    46
#define SPK_I2S_LRC     45
#define SPK_I2S_DOUT    42

// Status LED
#undef LED_BUILTIN
#define LED_BUILTIN     3

// I2S Configuration Defaults
#define I2S_SAMPLE_RATE 16000
#define I2S_BITS_PER_SAMPLE 16

// I2C Pins (Light Sensor LTR-308)
// Note: Shared with Camera SIOD/SIOC
#define I2C_SDA         8
#define I2C_SCL         9

// SD Card (SPI mode) — DFR1154 pin mapping
#define SD_CARD_CS   10
#define SD_CARD_MOSI 11
#define SD_CARD_SCK  12
#define SD_CARD_MISO 13

#endif  // BOARD_CONFIG_H
