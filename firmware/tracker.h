#pragma once
#include <Arduino.h>

// --- Lightweight Object Tracker (simplified ByteTrack-style) ---
// Maintains persistent IDs for detections across frames using:
//  1. Constant-velocity prediction (Kalman-lite, no full state matrix)
//  2. Greedy IoU/distance matching (Hungarian-lite)
//  3. Track lifecycle (tentative -> confirmed -> lost -> deleted)
//
// Designed for FOMO centroids (no width/height) and YOLO bounding boxes.
// Memory: ~1 KB SRAM for 16 tracks. Per-frame cost: ~2-5 ms.
//
// Use case: deduplicate person notifications across frames — same person
// stays the same track ID until they leave the scene for >N seconds.

#define TRACKER_MAX_TRACKS 16
#define TRACKER_MAX_AGE 30          // Frames without detection before track is deleted
#define TRACKER_MIN_HITS 2          // Hits before tentative -> confirmed
#define TRACKER_MAX_DISTANCE 80.0f  // Pixel distance for matching (frame coords)

enum TrackState {
    TRACK_TENTATIVE = 0,  // Just created, not yet confirmed
    TRACK_CONFIRMED = 1,  // Confirmed person, will trigger notifications
    TRACK_LOST = 2,       // Lost detection, predicting position
};

struct Track {
    int id;                  // Persistent unique ID (0 = unused slot)
    TrackState state;
    float cx, cy;            // Current center position (predicted or measured)
    float vx, vy;            // Velocity (pixels per frame)
    int age;                 // Frames since creation
    int hits;                // Total successful matches
    int time_since_update;   // Frames since last detection match
    float confidence;        // Last detection confidence
    bool notified;           // Set true after first notification (prevents duplicates)
};

class ObjectTracker {
public:
    ObjectTracker();

    // Update tracker with new detections from current frame.
    // detections: array of (cx, cy, confidence) tuples
    // n: number of detections
    // Returns: number of confirmed tracks after update
    int update(const float* detections_xyc, int n);

    // Get all confirmed tracks (caller iterates with getTrack)
    int getConfirmedCount() const { return confirmed_count; }

    // Get track by index (0..MAX_TRACKS-1), returns NULL if slot unused
    const Track* getTrack(int idx) const;

    // Reset all tracks (e.g. on motion clear)
    void reset();

    // Mark a track as notified (prevents duplicate Telegram for same person)
    void markNotified(int track_id);

    // Get next unnotified confirmed track (for notification logic)
    // Returns -1 if no unnotified tracks
    int getNextUnnotified() const;

private:
    Track tracks[TRACKER_MAX_TRACKS];
    int next_id;
    int confirmed_count;

    // Greedy assign detections to existing tracks by nearest neighbor
    void assignDetections(const float* detections_xyc, int n, int* assigned);

    // Create new track for unmatched detection
    int createTrack(float cx, float cy, float conf);

    // Predict next position using velocity
    void predictAll();
};

extern ObjectTracker objectTracker;
