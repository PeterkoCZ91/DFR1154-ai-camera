"""Statistics tracking for face recognition and detection metrics."""

import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime


class Statistics:
    def __init__(self, save_path: str = "stats.json", auto_save_interval: int = 300):
        self.save_path = save_path
        self.auto_save_interval = auto_save_interval
        self.lock = threading.RLock()  # RLock: save() calls get_summary()

        # Counters
        self.session_start = time.time()
        self.face_attempts = 0
        self.face_recognized = 0
        self.face_unknown = 0
        self.face_no_face = 0
        self.detections: defaultdict = defaultdict(int)

        # Motion detection statistics
        self.motion_esp32_events = 0
        self.motion_python_fallback = 0
        self.motion_true_positives = 0
        self.motion_false_positives = 0

        self._load()

        # Auto-save thread
        self.running = True
        self.save_thread = threading.Thread(target=self._auto_save_loop, daemon=True)
        self.save_thread.start()

    def _load(self) -> None:
        """Load existing cumulative statistics from file."""
        if not os.path.exists(self.save_path):
            return
        try:
            with open(self.save_path, "r") as f:
                data = json.load(f)
                if "cumulative" in data:
                    cum = data["cumulative"]
                    self.face_attempts = cum.get("face_attempts", 0)
                    self.face_recognized = cum.get("face_recognized", 0)
                    self.face_unknown = cum.get("face_unknown", 0)
                    self.face_no_face = cum.get("face_no_face", 0)
                    self.detections = defaultdict(int, cum.get("detections", {}))
        except Exception as e:
            print(f"Failed to load stats: {e}")

    def record_face_attempt(self, result: bool, name: str) -> None:
        with self.lock:
            self.face_attempts += 1
            if result and name not in ["Unknown", "No face", "Error", "Invalid frame"]:
                self.face_recognized += 1
            elif name == "Unknown":
                self.face_unknown += 1
            elif name == "No face":
                self.face_no_face += 1

    def record_detection(self, label: str) -> None:
        with self.lock:
            self.detections[label] += 1

    def record_motion_event(self, esp32_detected: bool, python_detected: bool, person_found: bool) -> None:
        with self.lock:
            if esp32_detected:
                self.motion_esp32_events += 1
            elif python_detected:
                self.motion_python_fallback += 1

            if person_found:
                self.motion_true_positives += 1
            else:
                self.motion_false_positives += 1

    def get_summary(self) -> dict:
        with self.lock:
            uptime = time.time() - self.session_start
            success_rate = (self.face_recognized / self.face_attempts * 100) if self.face_attempts > 0 else 0

            motion_total = self.motion_esp32_events + self.motion_python_fallback
            esp32_pct = (self.motion_esp32_events / motion_total * 100) if motion_total > 0 else 0
            accuracy = (self.motion_true_positives / motion_total * 100) if motion_total > 0 else 0

            return {
                "session": {
                    "uptime_seconds": int(uptime),
                    "uptime_formatted": f"{int(uptime // 3600)}h {int((uptime % 3600) // 60)}m",
                },
                "face_recognition": {
                    "attempts": self.face_attempts,
                    "recognized": self.face_recognized,
                    "unknown": self.face_unknown,
                    "no_face": self.face_no_face,
                    "success_rate": f"{success_rate:.1f}%",
                },
                "motion_detection": {
                    "esp32_events": self.motion_esp32_events,
                    "python_fallback": self.motion_python_fallback,
                    "total_events": motion_total,
                    "esp32_percentage": f"{esp32_pct:.1f}%",
                    "true_positives": self.motion_true_positives,
                    "false_positives": self.motion_false_positives,
                    "accuracy": f"{accuracy:.1f}%",
                },
                "detections": dict(self.detections),
            }

    def save(self) -> bool:
        with self.lock:
            summary = self.get_summary()
            data = {
                "last_updated": datetime.now().isoformat(),
                "session": summary["session"],
                "face_recognition": summary["face_recognition"],
                "detections": summary["detections"],
                "cumulative": {
                    "face_attempts": self.face_attempts,
                    "face_recognized": self.face_recognized,
                    "face_unknown": self.face_unknown,
                    "face_no_face": self.face_no_face,
                    "detections": dict(self.detections),
                },
            }

            try:
                with open(self.save_path, "w") as f:
                    json.dump(data, f, indent=2)
                return True
            except Exception as e:
                print(f"Failed to save stats: {e}")
                return False

    def _auto_save_loop(self) -> None:
        while self.running:
            time.sleep(self.auto_save_interval)
            if self.running:
                self.save()

    def stop(self) -> None:
        self.running = False
        if self.save_thread.is_alive():
            self.save_thread.join(timeout=1)
        self.save()

    def print_summary(self) -> None:
        summary = self.get_summary()
        print("=" * 60)
        print("A12 STATISTICS")
        print("=" * 60)
        print(f"Uptime: {summary['session']['uptime_formatted']}")
        print()
        print("Face Recognition:")
        fr = summary["face_recognition"]
        print(f"   Attempts:   {fr['attempts']}")
        print(f"   Recognized: {fr['recognized']} ({fr['success_rate']})")
        print(f"   Unknown:    {fr['unknown']}")
        print(f"   No Face:    {fr['no_face']}")
        print()
        md = summary["motion_detection"]
        if md["total_events"] > 0:
            print("Motion Detection:")
            print(f"   ESP32 Events:    {md['esp32_events']} ({md['esp32_percentage']})")
            print(f"   Python Fallback: {md['python_fallback']}")
            print(f"   True Positives:  {md['true_positives']}")
            print(f"   False Positives: {md['false_positives']}")
            print(f"   Accuracy:        {md['accuracy']}")
            print()
        if summary["detections"]:
            print("Detections:")
            for label, count in summary["detections"].items():
                print(f"   {label.capitalize()}: {count}")
        print("=" * 60)
