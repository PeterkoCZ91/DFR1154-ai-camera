#ifndef AUDIO_HANDLER_H
#define AUDIO_HANDLER_H

#include <Arduino.h>
#include "esp_http_server.h"
#include "board_config.h" // Includes definitions for I2S_SAMPLE_RATE, etc.
#include <driver/i2s_pdm.h>

// Audio buffer settings
#define AUDIO_BUFFER_SIZE   4096
#define AUDIO_STREAM_BUFFER_SIZE 1024
#define AUDIO_THRESHOLD_DB  50.0  // dB threshold for sound detection

// Initialize I2S PDM Microphone
bool initAudio();

// Get current audio level in dB
float getAudioLevel();

// Check if audio level exceeds threshold
bool isAudioDetected();

// Background monitoring task (optional)
void audioTask(void *parameter);

// WAV stream handler for web server
esp_err_t audioStreamHandler(httpd_req_t *req);

// Get I2S RX channel handle for direct audio reads (recording)
i2s_chan_handle_t getI2SRxHandle();

// Global variables
extern bool audio_enabled;
extern float current_audio_level;
extern unsigned long last_audio_detection;

#endif // AUDIO_HANDLER_H