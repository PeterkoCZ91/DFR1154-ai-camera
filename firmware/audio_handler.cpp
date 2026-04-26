/**
 * Audio Handler - I2S PDM Microphone Driver (ESP-IDF 5.x / Arduino Core 3.x)
 *
 * DFRobot DFR1154 FireBeetle 2 ESP32-S3 AI Camera
 * Integrated I2S PDM Microphone
 * CLK: GPIO 38, DATA: GPIO 39
 */

#include "audio_handler.h"
#include <math.h>
#include <lwip/sockets.h>
#include <driver/i2s_pdm.h> // New Driver for Core 3.x
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

// Global variables
bool audio_enabled = false;
float current_audio_level = 0.0;
unsigned long last_audio_detection = 0;

// audio_stream_buffer moved to local allocation in audioStreamHandler for thread safety
// audio_buffer used to be global; getAudioLevel() now uses a stack-local buffer
// so concurrent /audio-status calls and audioTask cannot corrupt each other.

// I2S Channel Handle (Core 3.x)
static i2s_chan_handle_t rx_handle = NULL;

// Single-reader gate for the I2S RX channel. The audio stream HTTP handler,
// recordingTask, and the optional audioTask/getAudioLevel all read from the
// same channel; without this lock i2s_channel_read internals race.
static SemaphoreHandle_t i2s_rx_mutex = NULL;

esp_err_t audioReadLocked(void* buf, size_t buf_size, size_t* bytes_read,
                          TickType_t timeout) {
    if (!rx_handle) return ESP_ERR_INVALID_STATE;
    if (i2s_rx_mutex && xSemaphoreTake(i2s_rx_mutex, timeout) != pdTRUE) {
        if (bytes_read) *bytes_read = 0;
        return ESP_ERR_TIMEOUT;
    }
    esp_err_t res = i2s_channel_read(rx_handle, buf, buf_size, bytes_read, timeout);
    if (i2s_rx_mutex) xSemaphoreGive(i2s_rx_mutex);
    return res;
}

// WAV Header Structure
typedef struct {
    char     riff[4];          // "RIFF"
    uint32_t fileSize;         // Total file size - 8
    char     wave[4];          // "WAVE"
    char     fmt[4];           // "fmt "
    uint32_t fmtSize;          // 16
    uint16_t audioFormat;      // 1 for PCM
    uint16_t numChannels;      // Mono = 1
    uint32_t sampleRate;
    uint32_t byteRate;         // sampleRate * numChannels * bitsPerSample/8
    uint16_t blockAlign;       // numChannels * bitsPerSample/8
    uint16_t bitsPerSample;
    char     data[4];          // "data"
    uint32_t dataSize;         // Total data size
} WavHeader;


// Helper function to create and send WAV header
esp_err_t sendWavHeader(httpd_req_t *req) {
    WavHeader header;
    memcpy(header.riff, "RIFF", 4);
    header.fileSize = 0; // Unknown size for live stream
    memcpy(header.wave, "WAVE", 4);
    memcpy(header.fmt, "fmt ", 4);
    header.fmtSize = 16;
    header.audioFormat = 1;
    header.numChannels = 1; // Mono
    header.sampleRate = I2S_SAMPLE_RATE;
    header.bitsPerSample = I2S_BITS_PER_SAMPLE;
    header.byteRate = header.sampleRate * header.numChannels * (header.bitsPerSample / 8);
    header.blockAlign = header.numChannels * (header.bitsPerSample / 8);
    memcpy(header.data, "data", 4);
    header.dataSize = 0; // Unknown size for live stream

    return httpd_resp_send_chunk(req, (const char*)&header, sizeof(WavHeader));
}


/**
 * Initialize I2S PDM Microphone using ESP-IDF 5.x Driver
 */
bool initAudio() {
    Serial.println("🎤 Initializing I2S PDM Microphone (Core 3.x)...");

    if (i2s_rx_mutex == NULL) {
        i2s_rx_mutex = xSemaphoreCreateMutex();
        if (!i2s_rx_mutex) {
            Serial.println("❌ Failed to create I2S RX mutex");
            return false;
        }
    }

    // 1. Create I2S Channel
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_AUTO, I2S_ROLE_MASTER);
    esp_err_t err = i2s_new_channel(&chan_cfg, NULL, &rx_handle);
    if (err != ESP_OK) {
        Serial.printf("❌ Failed to create I2S channel: %d\n", err);
        return false;
    }

    // 2. Configure PDM RX
    i2s_pdm_rx_config_t pdm_rx_cfg = {
        .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(I2S_SAMPLE_RATE),
        .slot_cfg = I2S_PDM_RX_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .clk = (gpio_num_t)MIC_I2S_CLK, // GPIO 38
            .din = (gpio_num_t)MIC_I2S_DATA, // GPIO 39
        },
    };

    // 3. Initialize Channel
    err = i2s_channel_init_pdm_rx_mode(rx_handle, &pdm_rx_cfg);
    if (err != ESP_OK) {
        Serial.printf("❌ Failed to init PDM RX mode: %d\n", err);
        // Cleanup if init fails
        i2s_del_channel(rx_handle);
        rx_handle = NULL;
        return false;
    }

    // 4. Enable Channel
    err = i2s_channel_enable(rx_handle);
    if (err != ESP_OK) {
        Serial.printf("❌ Failed to enable I2S channel: %d\n", err);
        i2s_del_channel(rx_handle);
        rx_handle = NULL;
        return false;
    }

    audio_enabled = true;
    Serial.println("✅ I2S PDM Microphone initialized successfully (Core 3.x)");
    Serial.printf("   Sample rate: %d Hz, Pins: CLK=%d, DATA=%d\n", 
                  I2S_SAMPLE_RATE, MIC_I2S_CLK, MIC_I2S_DATA);

    return true;
}

/**
 * WAV audio stream handler
 */
esp_err_t audioStreamHandler(httpd_req_t *req) {
    if (!audio_enabled || rx_handle == NULL) {
        Serial.println("⚠️ Audio stream requested but audio not enabled");
        return httpd_resp_send_500(req);
    }

    esp_err_t res = ESP_OK;
    size_t bytes_read = 0;

    // Local buffer per connection -- thread-safe for concurrent clients
    int16_t* local_stream_buf = (int16_t*)malloc(AUDIO_STREAM_BUFFER_SIZE * sizeof(int16_t));
    if (!local_stream_buf) {
        Serial.println("❌ Failed to allocate audio stream buffer");
        return httpd_resp_send_500(req);
    }

    Serial.println("🎤 Audio stream connected");

    // Set headers
    httpd_resp_set_type(req, "audio/x-wav");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

    // Set socket timeout
    int sockfd = httpd_req_to_sockfd(req);
    struct timeval send_timeout;
    send_timeout.tv_sec = 0;
    send_timeout.tv_usec = 500000; // 500ms timeout
    if (setsockopt(sockfd, SOL_SOCKET, SO_SNDTIMEO, &send_timeout, sizeof(send_timeout)) < 0) {
        Serial.println("⚠️ Failed to set audio socket send timeout");
    }

    // Send WAV header
    res = sendWavHeader(req);
    if (res != ESP_OK) {
        Serial.println("❌ Failed to send WAV header");
        free(local_stream_buf);
        return res;
    }

    // Stream loop
    size_t stream_buf_bytes = AUDIO_STREAM_BUFFER_SIZE * sizeof(int16_t);
    while (true) {
        // Read from I2S Channel via shared mutex (other readers: recording, level)
        esp_err_t result = audioReadLocked(local_stream_buf, stream_buf_bytes, &bytes_read, pdMS_TO_TICKS(1000));

        if (result != ESP_OK) {
            Serial.printf("❌ I2S read error: %d\n", result);
            res = ESP_FAIL;
            break;
        }

        if (bytes_read > 0) {
            res = httpd_resp_send_chunk(req, (const char*)local_stream_buf, bytes_read);
            if (res != ESP_OK) {
                break;
            }
        }
    }

    Serial.println("🎤 Audio stream disconnected");
    free(local_stream_buf);
    httpd_resp_send_chunk(req, NULL, 0);

    return res;
}


/**
 * Get I2S RX channel handle for direct audio reads (AVI recording)
 */
i2s_chan_handle_t getI2SRxHandle() {
    return rx_handle;
}

/**
 * Calculate RMS audio level in dB
 */
float getAudioLevel() {
    if (!audio_enabled || rx_handle == NULL) return 0.0;

    // Stack-local buffer so concurrent /audio-status calls do not stomp on
    // each other. 1024 samples (2 KB) is enough for an RMS estimate and keeps
    // the HTTP handler stack budget comfortable.
    const size_t kLevelSamples = 1024;
    int16_t buf[kLevelSamples];
    size_t bytes_read = 0;
    esp_err_t result = audioReadLocked(buf, sizeof(buf), &bytes_read, pdMS_TO_TICKS(100));

    if (result != ESP_OK || bytes_read == 0) {
        return 0.0;
    }

    int samples = bytes_read / sizeof(int16_t);
    if (samples == 0) return 0.0;

    long long sum_squares = 0; // Use long long to prevent overflow

    for (int i = 0; i < samples; i++) {
        int16_t sample = buf[i];
        sum_squares += sample * sample;
    }

    float rms = sqrt((float)sum_squares / samples);
    
    // Avoid log(0)
    if (rms < 1.0) rms = 1.0;

    float db = 20.0 * log10(rms / 32768.0);  // Normalize to 16-bit range (approx)
    
    // Clamp to reasonable range if needed, or just return raw dBFS (usually negative)
    // For visualization, we might want to offset it. 
    // Standard dBFS is 0 at max, -inf at silence.
    // Let's shift it up for easier positive number logic if expected by other code
    float display_db = db + 90.0; 

    return display_db;
}

/**
 * Check if audio level exceeds threshold
 */
bool isAudioDetected() {
    current_audio_level = getAudioLevel();

    if (current_audio_level > AUDIO_THRESHOLD_DB) {
        last_audio_detection = millis();
        return true;
    }

    return false;
}

/**
 * Audio monitoring task
 */
void audioTask(void *parameter) {
    Serial.println("🎵 Audio monitoring task started");

    while (true) {
        if (audio_enabled) {
            current_audio_level = getAudioLevel();
            
            if (current_audio_level > AUDIO_THRESHOLD_DB) {
                 // Optional: Log only if very loud or debug enabled
                 // Serial.printf("🔊 Audio detected: %.1f\n", current_audio_level);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}