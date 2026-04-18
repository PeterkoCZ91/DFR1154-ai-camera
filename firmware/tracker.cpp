#include "tracker.h"
#include <math.h>
#include <float.h>

ObjectTracker objectTracker;

ObjectTracker::ObjectTracker() : next_id(1), confirmed_count(0) {
    reset();
}

void ObjectTracker::reset() {
    for (int i = 0; i < TRACKER_MAX_TRACKS; i++) {
        tracks[i].id = 0;
        tracks[i].state = TRACK_TENTATIVE;
        tracks[i].cx = 0; tracks[i].cy = 0;
        tracks[i].vx = 0; tracks[i].vy = 0;
        tracks[i].age = 0;
        tracks[i].hits = 0;
        tracks[i].time_since_update = 0;
        tracks[i].confidence = 0;
        tracks[i].notified = false;
    }
    confirmed_count = 0;
}

void ObjectTracker::predictAll() {
    for (int i = 0; i < TRACKER_MAX_TRACKS; i++) {
        if (tracks[i].id == 0) continue;
        // Constant-velocity prediction
        tracks[i].cx += tracks[i].vx;
        tracks[i].cy += tracks[i].vy;
        tracks[i].age++;
        tracks[i].time_since_update++;
    }
}

int ObjectTracker::createTrack(float cx, float cy, float conf) {
    // Find empty slot
    for (int i = 0; i < TRACKER_MAX_TRACKS; i++) {
        if (tracks[i].id == 0) {
            tracks[i].id = next_id++;
            tracks[i].state = TRACK_TENTATIVE;
            tracks[i].cx = cx;
            tracks[i].cy = cy;
            tracks[i].vx = 0;
            tracks[i].vy = 0;
            tracks[i].age = 1;
            tracks[i].hits = 1;
            tracks[i].time_since_update = 0;
            tracks[i].confidence = conf;
            tracks[i].notified = false;
            return i;
        }
    }
    return -1;  // No free slots
}

void ObjectTracker::assignDetections(const float* detections_xyc, int n, int* assigned) {
    // Greedy nearest-neighbor matching
    // For each detection, find closest unassigned track within MAX_DISTANCE
    for (int d = 0; d < n; d++) {
        assigned[d] = -1;
        float best_dist = TRACKER_MAX_DISTANCE;
        int best_track = -1;

        float dx_query = detections_xyc[d * 3];
        float dy_query = detections_xyc[d * 3 + 1];

        for (int t = 0; t < TRACKER_MAX_TRACKS; t++) {
            if (tracks[t].id == 0) continue;

            // Skip if track already matched this frame
            bool already = false;
            for (int j = 0; j < d; j++) {
                if (assigned[j] == t) { already = true; break; }
            }
            if (already) continue;

            float dx = dx_query - tracks[t].cx;
            float dy = dy_query - tracks[t].cy;
            float dist = sqrtf(dx * dx + dy * dy);

            if (dist < best_dist) {
                best_dist = dist;
                best_track = t;
            }
        }
        assigned[d] = best_track;
    }
}

int ObjectTracker::update(const float* detections_xyc, int n) {
    // 1. Predict all existing tracks forward
    predictAll();

    // 2. Assign detections to tracks (greedy nearest-neighbor)
    int assigned[16];  // Max detections per frame; FOMO typically returns < 10
    if (n > 16) n = 16;
    assignDetections(detections_xyc, n, assigned);

    // 3. Update matched tracks with measurements
    for (int d = 0; d < n; d++) {
        int t = assigned[d];
        float mx = detections_xyc[d * 3];
        float my = detections_xyc[d * 3 + 1];
        float mc = detections_xyc[d * 3 + 2];

        if (t >= 0) {
            // Update velocity (smoothed) — predicted-to-measured delta
            float new_vx = mx - tracks[t].cx;
            float new_vy = my - tracks[t].cy;
            tracks[t].vx = 0.7f * tracks[t].vx + 0.3f * new_vx;
            tracks[t].vy = 0.7f * tracks[t].vy + 0.3f * new_vy;
            tracks[t].cx = mx;
            tracks[t].cy = my;
            tracks[t].confidence = mc;
            tracks[t].hits++;
            tracks[t].time_since_update = 0;

            // Promote to confirmed
            if (tracks[t].state == TRACK_TENTATIVE && tracks[t].hits >= TRACKER_MIN_HITS) {
                tracks[t].state = TRACK_CONFIRMED;
            }
            if (tracks[t].state == TRACK_LOST) {
                tracks[t].state = TRACK_CONFIRMED;
            }
        } else {
            // Unmatched detection → create new tentative track
            createTrack(mx, my, mc);
        }
    }

    // 4. Mark unmatched tracks as lost / delete old ones
    confirmed_count = 0;
    for (int t = 0; t < TRACKER_MAX_TRACKS; t++) {
        if (tracks[t].id == 0) continue;

        if (tracks[t].time_since_update > 0) {
            // Unmatched this frame
            if (tracks[t].state == TRACK_CONFIRMED) {
                tracks[t].state = TRACK_LOST;
            }
            if (tracks[t].time_since_update > TRACKER_MAX_AGE) {
                // Delete track — reset notified explicitly so recycled slots
                // can't leak the previous occupant's state even if a future
                // code path claims the slot without going through createTrack.
                tracks[t].id = 0;
                tracks[t].notified = false;
                continue;
            }
        }

        if (tracks[t].state == TRACK_CONFIRMED) confirmed_count++;
    }

    return confirmed_count;
}

const Track* ObjectTracker::getTrack(int idx) const {
    if (idx < 0 || idx >= TRACKER_MAX_TRACKS) return NULL;
    if (tracks[idx].id == 0) return NULL;
    return &tracks[idx];
}

void ObjectTracker::markNotified(int track_id) {
    for (int i = 0; i < TRACKER_MAX_TRACKS; i++) {
        if (tracks[i].id == track_id) {
            tracks[i].notified = true;
            return;
        }
    }
}

int ObjectTracker::getNextUnnotified() const {
    for (int i = 0; i < TRACKER_MAX_TRACKS; i++) {
        if (tracks[i].id != 0 && tracks[i].state == TRACK_CONFIRMED && !tracks[i].notified) {
            return tracks[i].id;
        }
    }
    return -1;
}
