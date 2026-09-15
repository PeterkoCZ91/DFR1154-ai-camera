"""Statistics tracking for face recognition and detection metrics."""

import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime

from . import scorer_client

from .face_result import FaceOutcome, FaceResult


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

    def record_face_attempt(self, result: FaceResult) -> None:
        """Count one check by what it established.

        NO_FACE must not land in the unknown-person bucket: it is the common
        case (78% of person frames) and it says nothing about who was there.
        ERROR and UNAVAILABLE are counted as attempts only — crediting them to
        anyone would overstate what the system knows.
        """
        with self.lock:
            self.face_attempts += 1
            if result.outcome is FaceOutcome.RESIDENT:
                self.face_recognized += 1
            elif result.outcome is FaceOutcome.STRANGER:
                self.face_unknown += 1
            elif result.outcome is FaceOutcome.NO_FACE:
                self.face_no_face += 1

    def record_detection(self, label: str) -> None:
        with self.lock:
            self.detections[label] += 1

    def record_motion_event(self, esp32_detected: bool, python_detected: bool, person_found: bool) -> None:
        """Record one motion-triggered check, and only a motion-triggered one.

        This is called once per YOLO invocation, and most of those are not
        motion events at all — the periodic sweep and the PIR window both get
        here with nothing moving. Counting those as motion outcomes put a
        different population in the numerator than in the denominator: the
        accuracy figure could read 0.0% beside thousands of "false positives"
        from checks that had no motion to be wrong about, and in a mixed
        configuration it could exceed 100%.
        """
        with self.lock:
            if esp32_detected:
                self.motion_esp32_events += 1
            elif python_detected:
                self.motion_python_fallback += 1
            else:
                return   # nothing moved; this check says nothing about motion

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
            # Over the outcomes themselves, so the ratio stays defined even if
            # the two ever drift apart — an invariant test pins that they do not.
            motion_judged = self.motion_true_positives + self.motion_false_positives
            accuracy = (self.motion_true_positives / motion_judged * 100) if motion_judged > 0 else 0

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
                "scorer": scorer_client.metrics_snapshot(),
            }

    def save(self) -> bool:
        with self.lock:
            summary = self.get_summary()
            data = {
                "last_updated": datetime.now().isoformat(),
                "session": summary["session"],
                "face_recognition": summary["face_recognition"],
                "detections": summary["detections"],
                "scorer": summary["scorer"],
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
