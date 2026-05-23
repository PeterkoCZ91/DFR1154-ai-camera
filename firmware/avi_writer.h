#ifndef AVI_WRITER_H
#define AVI_WRITER_H

#include <Arduino.h>
#include "FS.h"

/**
 * AVI Writer — RIFF/AVI 1.0 muxer for MJPEG video + PCM16 mono audio.
 *
 * Usage:
 *   AviWriter avi;
 *   avi.begin(file, width, height, fps, audioSampleRate);
 *   // loop:
 *     avi.writeVideoFrame(jpegData, jpegLen);
 *     avi.writeAudioChunk(pcmData, pcmBytes);
 *   avi.end();
 *
 * No heap allocation — all header data is stack/static.
 */
class AviWriter {
public:
    bool begin(File& file, uint16_t width, uint16_t height,
               uint8_t fps, uint32_t audioSampleRate);
    bool writeVideoFrame(const uint8_t* jpegData, size_t jpegLen);
    bool writeAudioChunk(const int16_t* pcmData, size_t pcmBytes);
    bool end();

    uint32_t getVideoFrames() const { return _videoFrames; }
    uint32_t getAudioChunks() const { return _audioChunks; }
    uint32_t getTotalBytes() const { return _totalDataBytes; }

private:
    File* _file = nullptr;
    uint16_t _width = 0;
    uint16_t _height = 0;
    uint8_t _fps = 10;
    uint32_t _audioRate = 16000;
    uint32_t _videoFrames = 0;
    uint32_t _audioChunks = 0;
    uint32_t _totalDataBytes = 0;  // bytes in 'movi' list (after LIST fourcc+size)
    uint32_t _moviStart = 0;       // file offset where 'movi' LIST size field is

    bool writeChunk(const char fourcc[4], const uint8_t* data, size_t len);
    void writePadByte(size_t len);
};

#endif // AVI_WRITER_H
