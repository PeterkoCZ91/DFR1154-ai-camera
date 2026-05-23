/**
 * AVI Writer — RIFF/AVI 1.0 muxer for MJPEG + PCM16 mono audio.
 *
 * Container layout:
 *   RIFF 'AVI '
 *     LIST 'hdrl'
 *       'avih'  — MainAVIHeader
 *       LIST 'strl' (video)
 *         'strh' — AVIStreamHeader (vids/MJPG)
 *         'strf' — BITMAPINFOHEADER
 *       LIST 'strl' (audio)
 *         'strh' — AVIStreamHeader (auds)
 *         'strf' — WAVEFORMATEX
 *     LIST 'movi'
 *       '00dc' — video chunks (JPEG)
 *       '01wb' — audio chunks (PCM16)
 *
 * begin() writes a placeholder header (all sizes zero).
 * end() seeks back and patches RIFF size, movi size, and frame counts.
 */

#include "avi_writer.h"
#include <string.h>

// Helper: write a little-endian uint32
static void put32(uint8_t* p, uint32_t v) {
    p[0] = v & 0xFF;
    p[1] = (v >> 8) & 0xFF;
    p[2] = (v >> 16) & 0xFF;
    p[3] = (v >> 24) & 0xFF;
}

// Helper: write a little-endian uint16
static void put16(uint8_t* p, uint16_t v) {
    p[0] = v & 0xFF;
    p[1] = (v >> 8) & 0xFF;
}

// Helper: copy fourcc
static void putCC(uint8_t* p, const char* cc) {
    memcpy(p, cc, 4);
}

bool AviWriter::begin(File& file, uint16_t width, uint16_t height,
                      uint8_t fps, uint32_t audioSampleRate) {
    _file = &file;
    _width = width;
    _height = height;
    _fps = fps > 0 ? fps : 10;
    _audioRate = audioSampleRate;
    _videoFrames = 0;
    _audioChunks = 0;
    _totalDataBytes = 0;

    // --- Build entire header in a stack buffer ---
    // RIFF(12) + LIST hdrl(12) + avih(64) +
    // LIST strl_v(12) + strh_v(64) + strf_v(48) +
    // LIST strl_a(12) + strh_a(64) + strf_a(26) +
    // LIST movi(12)
    // Total: 12+12+64 + 12+64+48 + 12+64+26 + 12 = 326 bytes
    const size_t HDR_SIZE = 326;
    uint8_t hdr[HDR_SIZE];
    memset(hdr, 0, HDR_SIZE);

    uint32_t usPerFrame = 1000000 / _fps;
    uint16_t audioBlockAlign = 2;  // 16-bit mono = 2 bytes per sample
    uint32_t audioBytesPerSec = _audioRate * audioBlockAlign;

    size_t p = 0;

    // --- RIFF header ---
    putCC(hdr + p, "RIFF"); p += 4;
    put32(hdr + p, 0);      p += 4;  // placeholder: total file size - 8
    putCC(hdr + p, "AVI "); p += 4;

    // --- LIST 'hdrl' ---
    putCC(hdr + p, "LIST"); p += 4;
    // hdrl size: avih(64) + strl_v(12+64+48) + strl_a(12+64+26) = 290
    put32(hdr + p, 4 + 64 + (12+64+48) + (12+64+26)); p += 4;
    putCC(hdr + p, "hdrl"); p += 4;

    // --- avih (MainAVIHeader) ---
    putCC(hdr + p, "avih"); p += 4;
    put32(hdr + p, 56);     p += 4;  // struct size
    put32(hdr + p, usPerFrame); p += 4;  // dwMicroSecPerFrame
    put32(hdr + p, 0);      p += 4;  // dwMaxBytesPerSec (0=unknown)
    put32(hdr + p, 0);      p += 4;  // dwPaddingGranularity
    put32(hdr + p, 0x10);   p += 4;  // dwFlags: AVIF_HASINDEX=0x10 (we don't write idx1, but flag is harmless)
    put32(hdr + p, 0);      p += 4;  // dwTotalFrames — patched in end()
    put32(hdr + p, 0);      p += 4;  // dwInitialFrames
    put32(hdr + p, 2);      p += 4;  // dwStreams (video + audio)
    put32(hdr + p, 0);      p += 4;  // dwSuggestedBufferSize
    put32(hdr + p, _width); p += 4;  // dwWidth
    put32(hdr + p, _height);p += 4;  // dwHeight
    p += 16;  // dwReserved[4]

    // --- LIST 'strl' (video stream) ---
    putCC(hdr + p, "LIST"); p += 4;
    put32(hdr + p, 4 + 64 + 48); p += 4;  // list payload size
    putCC(hdr + p, "strl"); p += 4;

    // strh (AVIStreamHeader — video)
    putCC(hdr + p, "strh"); p += 4;
    put32(hdr + p, 56);     p += 4;  // struct size
    putCC(hdr + p, "vids"); p += 4;  // fccType
    putCC(hdr + p, "MJPG"); p += 4;  // fccHandler
    put32(hdr + p, 0);      p += 4;  // dwFlags
    put16(hdr + p, 0);      p += 2;  // wPriority
    put16(hdr + p, 0);      p += 2;  // wLanguage
    put32(hdr + p, 0);      p += 4;  // dwInitialFrames
    put32(hdr + p, 1);      p += 4;  // dwScale
    put32(hdr + p, _fps);   p += 4;  // dwRate (fps)
    put32(hdr + p, 0);      p += 4;  // dwStart
    put32(hdr + p, 0);      p += 4;  // dwLength — patched in end()
    put32(hdr + p, 0);      p += 4;  // dwSuggestedBufferSize
    put32(hdr + p, 0);      p += 4;  // dwQuality
    put32(hdr + p, 0);      p += 4;  // dwSampleSize
    put16(hdr + p, 0); p += 2;  // rcFrame left
    put16(hdr + p, 0); p += 2;  // rcFrame top
    put16(hdr + p, _width); p += 2;  // rcFrame right
    put16(hdr + p, _height); p += 2; // rcFrame bottom

    // strf (BITMAPINFOHEADER — video)
    putCC(hdr + p, "strf"); p += 4;
    put32(hdr + p, 40);     p += 4;  // struct size
    put32(hdr + p, 40);     p += 4;  // biSize
    put32(hdr + p, _width); p += 4;  // biWidth
    put32(hdr + p, _height);p += 4;  // biHeight
    put16(hdr + p, 1);      p += 2;  // biPlanes
    put16(hdr + p, 24);     p += 2;  // biBitCount (24-bit for MJPEG)
    putCC(hdr + p, "MJPG"); p += 4;  // biCompression
    put32(hdr + p, _width * _height * 3); p += 4; // biSizeImage
    p += 16;  // biXPels, biYPels, biClrUsed, biClrImportant = 0

    // --- LIST 'strl' (audio stream) ---
    putCC(hdr + p, "LIST"); p += 4;
    put32(hdr + p, 4 + 64 + 26); p += 4;  // list payload size
    putCC(hdr + p, "strl"); p += 4;

    // strh (AVIStreamHeader — audio)
    putCC(hdr + p, "strh"); p += 4;
    put32(hdr + p, 56);     p += 4;  // struct size
    putCC(hdr + p, "auds"); p += 4;  // fccType
    put32(hdr + p, 0);      p += 4;  // fccHandler (0 for PCM)
    put32(hdr + p, 0);      p += 4;  // dwFlags
    put16(hdr + p, 0);      p += 2;  // wPriority
    put16(hdr + p, 0);      p += 2;  // wLanguage
    put32(hdr + p, 0);      p += 4;  // dwInitialFrames
    put32(hdr + p, 1);      p += 4;  // dwScale (1 for audio)
    put32(hdr + p, _audioRate); p += 4; // dwRate (samples/sec)
    put32(hdr + p, 0);      p += 4;  // dwStart
    put32(hdr + p, 0);      p += 4;  // dwLength — patched in end()
    put32(hdr + p, 0);      p += 4;  // dwSuggestedBufferSize
    put32(hdr + p, 0);      p += 4;  // dwQuality
    put32(hdr + p, audioBlockAlign); p += 4; // dwSampleSize
    p += 8;  // rcFrame (unused for audio)

    // strf (WAVEFORMATEX — audio)
    putCC(hdr + p, "strf"); p += 4;
    put32(hdr + p, 18);     p += 4;  // struct size
    put16(hdr + p, 1);      p += 2;  // wFormatTag = WAVE_FORMAT_PCM
    put16(hdr + p, 1);      p += 2;  // nChannels = mono
    put32(hdr + p, _audioRate); p += 4; // nSamplesPerSec
    put32(hdr + p, audioBytesPerSec); p += 4; // nAvgBytesPerSec
    put16(hdr + p, audioBlockAlign); p += 2; // nBlockAlign
    put16(hdr + p, 16);     p += 2;  // wBitsPerSample
    put16(hdr + p, 0);      p += 2;  // cbSize (extra format bytes)

    // --- LIST 'movi' ---
    putCC(hdr + p, "LIST"); p += 4;
    _moviStart = p;  // remember offset for patching movi size
    put32(hdr + p, 0);      p += 4;  // placeholder: movi list size
    putCC(hdr + p, "movi"); p += 4;

    // Sanity check
    if (p != HDR_SIZE) {
        Serial.printf("AVI: header size mismatch: %u != %u\n", (unsigned)p, (unsigned)HDR_SIZE);
        return false;
    }

    size_t written = _file->write(hdr, HDR_SIZE);
    return written == HDR_SIZE;
}

bool AviWriter::writeVideoFrame(const uint8_t* jpegData, size_t jpegLen) {
    if (!writeChunk("00dc", jpegData, jpegLen)) return false;
    _videoFrames++;
    return true;
}

bool AviWriter::writeAudioChunk(const int16_t* pcmData, size_t pcmBytes) {
    if (!writeChunk("01wb", (const uint8_t*)pcmData, pcmBytes)) return false;
    _audioChunks++;
    return true;
}

bool AviWriter::writeChunk(const char fourcc[4], const uint8_t* data, size_t len) {
    if (!_file) return false;

    uint8_t chunkHdr[8];
    memcpy(chunkHdr, fourcc, 4);
    put32(chunkHdr + 4, (uint32_t)len);

    if (_file->write(chunkHdr, 8) != 8) return false;
    if (len > 0) {
        if (_file->write(data, len) != len) return false;
    }

    _totalDataBytes += 8 + len;

    // AVI chunks must be 2-byte aligned; pad with zero if odd
    if (len & 1) {
        writePadByte(len);
        _totalDataBytes += 1;
    }

    return true;
}

void AviWriter::writePadByte(size_t len) {
    if (len & 1) {
        uint8_t zero = 0;
        _file->write(&zero, 1);
    }
}

bool AviWriter::end() {
    if (!_file) return false;

    // Calculate sizes for patching
    uint32_t moviSize = 4 + _totalDataBytes;  // 'movi' fourcc + chunk data
    uint32_t riffSize = _moviStart + 4 + moviSize + 4;  // header + movi list
    // Actually: riffSize = fileSize - 8
    // fileSize = HDR_SIZE + _totalDataBytes
    // But simpler: get actual file position
    size_t fileEnd = _file->position();
    uint32_t riffSizeVal = (uint32_t)(fileEnd - 8);

    // Patch RIFF size at offset 4
    _file->seek(4);
    uint8_t buf[4];
    put32(buf, riffSizeVal);
    _file->write(buf, 4);

    // Patch avih.dwTotalFrames at offset 12+12+8+16 = 48
    // RIFF(12) + LIST(8) + 'hdrl'(4) + 'avih'(4) + size(4) + usPerFrame(4) + maxBPS(4) + pad(4) + flags(4) + totalFrames
    // = 12 + 8 + 4 + 4 + 4 + 4 + 4 + 4 + 4 + 4 = offset 48 from start? Let me recalculate:
    // RIFF 'AVI ' = 12 bytes (offset 0-11)
    // LIST hdrl = 12 bytes (offset 12-23)
    // avih chunk: 'avih'(4) + size(4) + data(56) at offset 24
    //   data starts at offset 32
    //   dwTotalFrames is at data+16 = offset 48
    _file->seek(48);
    put32(buf, _videoFrames);
    _file->write(buf, 4);

    // Patch video strh.dwLength
    // After avih(64): offset 24+8+56 = 88
    // LIST strl video: 12 bytes, offset 88-99
    // strh video: 'strh'(4)+size(4) at offset 100, data at 108
    //   dwLength is at data+32 = offset 140
    _file->seek(140);
    put32(buf, _videoFrames);
    _file->write(buf, 4);

    // Patch audio strh.dwLength
    // After video strl: 88 + 12 + 64 + 48 = 212
    // LIST strl audio: 12 bytes, offset 212-223
    // strh audio: 'strh'(4)+size(4) at offset 224, data at 232
    //   dwLength is at data+32 = offset 264
    _file->seek(264);
    // Audio length in samples
    uint32_t audioSamples = _audioChunks > 0 ?
        (_audioChunks * (_audioRate / _fps)) : 0;
    put32(buf, audioSamples);
    _file->write(buf, 4);

    // Patch movi LIST size
    _file->seek(_moviStart);
    put32(buf, moviSize);
    _file->write(buf, 4);

    _file->flush();
    _file = nullptr;
    return true;
}
