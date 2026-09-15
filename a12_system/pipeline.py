"""Detection pipeline: per-frame processing with sensor fusion and notifications."""

import logging
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime

import cv2
import numpy as np

from .face_result import (
    FaceEpisode,
    FaceResult,
    debug_crop_name,
    notification_name,
    should_run_face_check,
)
from .detection import crop_person_box
from .flat_episode import FlatEpisodeState


def retention_days(value: float, floor: float = 0.0) -> float:
    """Turn a configured retention into a day count for the media sweep.

    Every retention knob A12 documents treats 0 as "keep indefinitely", so it
    maps to infinity here. Reading that 0 as a literal day count is what made
    `MEDIA_RETENTION_DAYS=0` delete media after the 0.25-day floor, and would
    make `DECISION_AUDIT_RETENTION_DAYS=0` — the one setting that keeps audit
    rows forever — wipe the candidate images those rows point at.
    """
    days = float(value)
    if days <= 0:
        return float("inf")
    return max(floor, days)


# A decision reached without any sensor behind it carries no independent claim
# that something was there, so a "nobody here" verdict from it is not evidence
# of anything. Only a sensor-triggered miss is worth an image.
UNTRIGGERED_SOURCES = frozenset({"periodic"})


# Outcomes that already save their own person media. A candidate copy of the
# same frame would only duplicate the clip, and because snapshots share one
# rate-limit slot it would also crowd out the unconfirmed candidate a second
# later — the exact frame the candidates folder exists to preserve.
OUTCOMES_WITH_OWN_MEDIA = frozenset({"recorded_and_notified", "recorded_local_only"})


def classify_frame_health(
    brightness: float, std: float, dark_threshold: float, flat_std_threshold: float
) -> str | None:
    """Classify image appearance, not the cause: dark, flat, or textured.

    Darkness, a featureless scene, and sensor faults can all produce uniform
    pixels. These statistics alone cannot establish that a reboot is needed.
    """
    if brightness < dark_threshold:
        return "dark"
    if std < flat_std_threshold:
        return "flat"
    return None


def frames_are_identical(previous, current) -> bool:
    """Did the sensor read out a new frame, or repeat the last one byte for byte?

    Darkness and a stopped readout both produce near-zero standard deviation,
    so the per-frame statistics above cannot separate them — on 2026-09-14 an
    unlit hallway spent the whole AEC/AGC budget and reached CRITICAL while the
    camera was working correctly. Frame-to-frame change does separate them:
    silicon always carries read noise, so a live sensor never repeats a frame
    exactly, while a hung one repeats it for minutes.

    Measured that evening on this camera, at the 160x120 the heartbeat already
    computes -- hung sensor: max|d| 0.00; the closest live case (dark hallway,
    AGC off): max|d| 1.00. Hence equality rather than a threshold: any margin
    would have to be fitted to a scene, and the gap is already a full
    quantisation step.

    Without a previous frame nothing is established, so the answer is False.
    "Unknown" must never read as "frozen", or the first heartbeat after every
    A12 start would count towards rebooting the camera.
    """
    if previous is None or current is None:
        return False
    # array_equal compares shape as well, so a resolution change mid-episode
    # (a frame_size write reboots the camera) reads as "not identical" rather
    # than raising.
    return bool(np.array_equal(previous, current))


def flat_recovery_action(
    consecutive_flat: int,
    reconnect_strikes: int,
    forced_reconnects: int,
    reboots: int,
    reconnect_before_reboot: int,
    max_reboots: int,
    now: float,
    last_action: float,
    cooldown: float,
) -> str:
    """Generic bounded escalation ladder, driven by a repeat count.

    Used by the stream-freeze watchdog: repeated freezes/stream-ends without a
    healthy gap between them are strong evidence of a stuck transport, unlike
    ambiguous image statistics (uniform frames can just mean darkness), so only
    freezes drive this ladder now. Escalation, each step rate-limited by
    ``cooldown``:

    - ``"none"``      — below the strike threshold or within the cooldown.
    - ``"reconnect"`` — tear down and rebuild the stream connection (clears an
      A12-side stale-connection failure mode). Unused by the freeze caller
      (it passes a fixed ``forced_reconnects=0``, ``reconnect_before_reboot=0``),
      kept generic for callers that do want a reconnect step first.
    - ``"reboot"``    — reboot the camera over the LAN (a soft ESP.restart has
      cleared a wedged OV3660 before — verified 2026-07-11 — though not always:
      an AEC-wedge case on 2026-09-11 survived 5 LAN reboots and needed a
      physical power-cycle instead).
    - ``"giveup"``    — even ``max_reboots`` camera reboots didn't recover; likely
      dead hardware. Stop acting and let the caller alert once.
    """
    if consecutive_flat < reconnect_strikes:
        return "none"
    if (now - last_action) < cooldown:
        return "none"
    if forced_reconnects < reconnect_before_reboot:
        return "reconnect"
    if reboots < max_reboots:
        return "reboot"
    return "giveup"


def aec_unwedge_action(
    consecutive_flat: int,
    strikes: int,
    attempts: int,
    max_attempts: int,
    now: float,
    last_action: float,
    cooldown: float,
) -> str:
    """Decide whether to rewrite the camera's AEC/AGC registers.

    A wedged OV3660 exposure loop streams decodable but uniform JPEGs, so the
    freeze watchdog never trips. Re-applying the day/night profile does not
    clear it either (verified 2026-09-12: the 06:00 NIGHT->DUSK switch moved
    brightness 5.0 -> 64.1 and detail still never came back). Toggling AEC/AGC
    off and back on does, at the cost of two HTTP writes and no downtime —
    cheaper and, on the 2026-09-11 episode, more effective than the LAN reboot
    this replaced, which that wedge survived five times.

    - ``"none"``    — below the strike threshold, or inside the cooldown.
    - ``"unwedge"`` — rewrite AEC/AGC.
    - ``"giveup"``  — ``max_attempts`` rewrites did not restore detail; the
      cause is not the exposure loop. Stop writing and let the caller alert.
    """
    if consecutive_flat < strikes:
        return "none"
    if attempts >= max_attempts:
        return "giveup"
    if (now - last_action) < cooldown:
        return "none"
    return "unwedge"


def box_iou(first, second) -> float | None:
    """Return intersection-over-union for two xyxy boxes, or None without both."""
    if first is None or second is None:
        return None
    x0 = max(first[0], second[0])
    y0 = max(first[1], second[1])
    x1 = min(first[2], second[2])
    y1 = min(first[3], second[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    if not intersection:
        return 0.0
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union > 0 else 0.0


class DetectionPipeline:
    """Orchestrates motion detection, YOLO inference, sensor fusion, and notifications."""

    def __init__(
        self,
        runtime_config,
        detector,
        notifier,
        mqtt_client,
        db,
        stats,
        audio_monitor,
        ha_monitor,
        force_yolo_event: threading.Event,
        shared_state: dict,
        status_monitor,
        script_dir: str,
    ):
        self.runtime_config = runtime_config
        self.detector = detector
        self.notifier = notifier
        self.mqtt_client = mqtt_client
        self.db = db
        self.stats = stats
        self.audio_monitor = audio_monitor
        self.ha_monitor = ha_monitor
        self.force_yolo_event = force_yolo_event
        self.shared_state = shared_state
        self.status_monitor = status_monitor
        self.running = True
        self.camera_id = str(runtime_config.get("camera_id", "esp32_cam")).strip() or "esp32_cam"
        self.camera_name = str(runtime_config.get("camera_name", self.camera_id)).strip() or self.camera_id
        self.telegram_label = str(runtime_config.get("telegram.camera_label", self.camera_name)).strip()
        self.log_prefix = f"[{self.camera_id}:{self.camera_name}]"

        # Frame buffer for GIF/MP4 creation. Keep it small and PIR-gated:
        # full-resolution decoded camera frames are too expensive to retain.
        self.clip_fps = max(1, int(runtime_config.get("clip_buffer_fps", 5)))
        self.clip_frame_size = (
            max(160, int(runtime_config.get("clip_frame_width", 640))),
            max(120, int(runtime_config.get("clip_frame_height", 480))),
        )
        # Separate size for Telegram preview — smaller than local clip buffer
        self.telegram_preview_size = (
            int(runtime_config.get("telegram_preview_width", 640)),
            int(runtime_config.get("telegram_preview_height", 480)),
        )
        self.last_clip_buffer_sample = 0.0
        self.clip_buffer_interval = 1.0 / self.clip_fps
        self.recording_buffer_until = 0.0
        self.clip_pre_seconds = max(
            0, int(runtime_config.get("clip_pre_seconds", runtime_config.get("gif.duration_seconds", 5)))
        )
        self.post_buffer_seconds = max(0, int(runtime_config.get("clip_post_seconds", 15)))
        buffer_seconds = self.clip_pre_seconds + self.post_buffer_seconds + 2
        self.frame_buffer: deque = deque(maxlen=max(1, self.clip_fps * buffer_seconds))

        # How far past the trigger post frames can still be recovered from the rolling
        # deque. It holds `clip_fps * buffer_seconds` samples and is filled at clip_fps
        # during an active window, so it retains `buffer_seconds` seconds of history —
        # no more. adaptive_clip.max_post_seconds may ask to wait up to 60 s, but post
        # frames are read from the *live* deque after that wait, so anything older than
        # buffer_seconds had already been evicted: the clip came out as pre-frames
        # (held by reference, so they survived) plus only the last ~buffer_seconds,
        # with a silent gap of tens of seconds in the middle.
        #
        # Clamp the wait to what can actually be delivered: a shorter continuous clip
        # beats a longer one with a hole. To get longer clips raise clip_post_seconds,
        # which sizes the buffer — raising max_post_seconds on its own cannot help.
        self._max_retainable_post_seconds = max(1, buffer_seconds - 1)
        self._logged_post_clamp = False

        # Screenshot folder
        self.screenshot_folder = os.path.join(script_dir, "screenshots")
        os.makedirs(self.screenshot_folder, exist_ok=True)

        # Cooldowns
        self.last_save_time: dict[str, float] = {}
        self.cooldown_seconds = runtime_config.get("detection_cooldown_seconds", 5)

        # Counters (thread-safe)
        self._counter_lock = threading.Lock()
        self.motion_frame_counter = 0
        self.yolo_check_interval = runtime_config.get("yolo.check_interval", 5)
        self.person_notify_confidence = runtime_config.get(
            "yolo.notify_confidence_threshold",
            runtime_config.get("yolo.confidence_threshold", 0.55),
        )
        self.pir_person_notify_confidence = runtime_config.get(
            "yolo.pir_notify_confidence_threshold",
            min(self.person_notify_confidence, 0.45),
        )
        self.person_confirmations_required = max(
            1, int(runtime_config.get("yolo.person_confirmations", 2))
        )
        self.pir_person_confirmations_required = max(
            1, int(runtime_config.get("yolo.pir_person_confirmations", 1))
        )
        self.configure_face_checks(runtime_config.get("face_recognition", {}) or {})

        self.person_confirmation_streaks = {"camera": 0, "pir": 0}
        self.person_confirmation_boxes = {"camera": None, "pir": None}
        self.person_confirmation_seen_at = {"camera": 0.0, "pir": 0.0}
        self.person_confirmation_iou = max(
            0.0, min(1.0, float(runtime_config.get("yolo.person_confirmation_iou", 0.10)))
        )
        self.person_confirmation_max_gap = max(
            0.0, float(runtime_config.get("yolo.person_confirmation_max_gap_seconds", 8.0))
        )

        # FPS tracking
        # Do not emit a misleading startup heartbeat with Frames: 1. The first
        # heartbeat should describe a real interval after the stream is running.
        self.last_heartbeat = time.time()
        self.heartbeat_interval = 30
        self.frame_count = 0
        self.stream_fps = 10  # updated from heartbeat

        # Periodic YOLO
        self.last_periodic_yolo = 0
        self.periodic_yolo_interval = runtime_config.get("periodic_yolo_interval", 30)
        self.external_yolo_interval = float(
            runtime_config.get("external_trigger_yolo_interval_seconds", 2.0)
        )
        self.last_external_yolo = 0.0

        # Event scoring
        self.event_scoring_enabled = bool(runtime_config.get("event_scoring.enabled", True))
        self.event_notify_threshold = int(runtime_config.get("event_scoring.notify_threshold", 70))
        self.event_local_record_threshold = int(
            runtime_config.get("event_scoring.local_record_threshold", 45)
        )
        self.require_sensor_for_recording = bool(
            runtime_config.get("require_sensor_for_recording", True)
        )
        self.pir_recording_enabled = bool(runtime_config.get("pir_recording.enabled", True))
        self.pir_recording_label = str(runtime_config.get("pir_recording.label", "motion")).strip() or "motion"
        self.pir_recording_send_telegram = bool(
            runtime_config.get("pir_recording.send_telegram", True)
        )
        self.pir_recording_bypass_cooldown = bool(
            runtime_config.get("pir_recording.bypass_cooldown", True)
        )
        self.pir_recording_require_yolo = bool(
            runtime_config.get("pir_recording.require_yolo_for_telegram", False)
        )
        self.pir_recording_cooldown = max(
            0, int(runtime_config.get("pir_recording.cooldown_seconds", 30))
        )
        self.last_pir_record_time = 0.0
        self.last_pir_sensor_activity_recorded = 0.0

        # Media cleanup
        self.last_cleanup = 0.0
        self.cleanup_interval = max(
            60, int(runtime_config.get("media_cleanup_interval_seconds", 3600))
        )
        # The 0.25 floor only stops a small non-zero value from sweeping media
        # away within minutes; 0 still means "keep forever" as documented.
        self.cleanup_max_age_days = retention_days(
            runtime_config.get("media_retention_days", 2), 0.25
        )
        self.decision_audit_retention_days = max(
            0.0, float(runtime_config.get("decision_audit_retention_days", 30))
        )
        # Person media and candidate snapshots are learning data: they must
        # outlive the default 2-day media sweep, or week-old audit rows point
        # at deleted files.
        self.person_media_retention_days = retention_days(
            runtime_config.get("person_media_retention_days", 30),
            self.cleanup_max_age_days,
        )
        self.candidate_snapshot_enabled = bool(
            runtime_config.get("candidate_snapshot_enabled", True)
        )
        self.candidate_snapshot_min_interval = max(
            0.0, float(runtime_config.get("candidate_snapshot_min_interval_seconds", 1.0))
        )
        self._last_candidate_snapshot = 0.0
        self.miss_snapshot_enabled = bool(
            runtime_config.get("miss_snapshot_enabled", True)
        )
        # Misses get their own limiter: sharing one with candidates would let a
        # busy candidate stream starve the population that has no other record.
        self.miss_snapshot_min_interval = max(
            0.0, float(runtime_config.get("miss_snapshot_min_interval_seconds", 5.0))
        )
        self._last_miss_snapshot = 0.0

        # Async notification worker
        queue_maxsize = max(1, int(runtime_config.get("notification_queue_maxsize", 10)))
        self.notification_queue: queue.Queue = queue.Queue(maxsize=queue_maxsize)
        self._notify_thread = threading.Thread(target=self._notification_worker, daemon=True)
        self._notify_thread.start()

        # Groq vision face recognition
        self.groq_vision = None
        groq_api_key = runtime_config.get("groq_api_key", "")
        groq_faces_dir = os.path.join(script_dir, "known_faces")
        if groq_api_key:
            from .groq_vision import GroqVision
            self.groq_vision = GroqVision(groq_api_key, groq_faces_dir)
            logging.info(f"{self.log_prefix} Groq vision face recognition enabled")
        else:
            logging.info(f"{self.log_prefix} Groq vision disabled (no GROQ_API_KEY)")

        # HA URL for Nuki unlock
        self.ha_url = runtime_config.get("home_assistant_url", "")
        self.ha_token = runtime_config.get("home_assistant_token", "")
        self.nuki_entity_id = runtime_config.get("nuki_lock_entity_id", "lock.nuki_smart_lock")

        # Suppress motion Telegram when known person was recently identified
        self._known_person_until = 0.0

        # Brightness watchdog — detects AEC freeze (OV3660 UXGA issue)
        self.configure_frame_health_watchdog(runtime_config)
        # Persist notification timestamps so restarting A12 cannot flood the chat.
        self.flat_state = FlatEpisodeState(os.path.join(script_dir, "flat_episode_state.json"))

        # Stream-freeze escalation: a "Stream frozen"/"stream_ended" break
        # already forces a reconnect (the __main__ loop redials immediately),
        # so unlike the flat-frame ladder there is no separate reconnect rung
        # — only whether freezes keep recurring close together for long enough
        # to call it a storm rather than background noise. The camera's own
        # health telemetry (send_fail_count climbing, last_errno=104,
        # uptime_seconds never resetting) points at a wedged socket/heap state
        # on the ESP32 itself that reconnecting cannot clear — only a device
        # reboot does.
        self._freeze_reboot_after = int(runtime_config.get("stream_freeze_reboot_after", 5))
        self._freeze_max_reboots = int(runtime_config.get("stream_freeze_max_reboots", 3))
        self._freeze_healthy_gap = float(runtime_config.get("stream_freeze_healthy_gap_seconds", 600))
        self._freeze_action_cooldown = float(runtime_config.get("stream_freeze_reboot_cooldown", 120))
        self._freeze_notify_interval = float(runtime_config.get("stream_freeze_notify_interval", 3600))
        self._freeze_consecutive_count = 0
        self._last_freeze_time = 0.0
        self._last_freeze_action = 0.0
        self._freeze_healthy_frames = 0
        self.freeze_state = FlatEpisodeState(os.path.join(script_dir, "stream_freeze_state.json"))

    def _trigger_nuki_unlock(self, name: str):
        if not self.ha_url or not self.ha_token:
            logging.warning(f"{self.log_prefix} Nuki unlock skipped — no HA config")
            return
        try:
            import requests as _req
            _req.post(
                f"{self.ha_url}/api/services/lock/unlock",
                headers={"Authorization": f"Bearer {self.ha_token}", "Content-Type": "application/json"},
                json={"entity_id": self.nuki_entity_id},
                timeout=5,
            )
            logging.info(f"{self.log_prefix} Nuki unlock triggered for '{name}'")
            self.notifier.send_telegram(f"Odemknuto pro {name}", bypass_cooldown=True)
        except Exception as e:
            logging.error(f"{self.log_prefix} Nuki unlock failed: {e}")

    def _telegram_message(self, message: str) -> str:
        if self.telegram_label:
            return f"{self.telegram_label}: {message}"
        return message

    def _media_name(self, label: str, timestamp: str, suffix: str) -> str:
        return f"{self.camera_id}_{label}_{timestamp}{suffix}"

    def _send_recovery_snapshot(self, frame) -> None:
        try:
            tmp_path = os.path.join(self.screenshot_folder, f"{self.camera_id}_recovery.jpg")
            cv2.imwrite(tmp_path, frame)
            self.notifier.send_telegram(
                self._telegram_message("Camera view after recovery:"), media_path=tmp_path, bypass_cooldown=True
            )
        except Exception as e:
            logging.warning(f"{self.log_prefix} Recovery snapshot failed: {e}")

    def configure_frame_health_watchdog(self, runtime_config) -> None:
        """Read every frame-health knob and reset its counters.

        Split out of __init__ so the wiring is reachable without building the
        whole pipeline (__init__ starts threads and creates directories). Test
        harnesses call this instead of re-declaring the attribute list by hand,
        so a knob that __init__ forgets can no longer pass the suite.
        """
        self._dark_frame_threshold = int(runtime_config.get("brightness_watchdog_threshold", 30))
        self._dark_consecutive_required = int(runtime_config.get("brightness_watchdog_strikes", 1))
        self._flat_frame_std_threshold = float(runtime_config.get("flat_frame_std_threshold", 1.0))
        self._dark_consecutive_count = 0
        self._last_exposure_reset = 0.0
        # Image appearance cannot prove a sensor fault, especially at night, so
        # the only recovery driven from here is a non-disruptive AEC/AGC rewrite.
        self._flat_reconnect_strikes = int(runtime_config.get("flat_frame_reconnect_strikes", 5))
        self._flat_healthy_required = int(runtime_config.get("flat_frame_healthy_required", 10))
        # AEC/AGC rewrite budget. Cheap (two POSTs, no outage) and a no-op on a
        # genuinely dark but healthy scene, so it is safe on ambiguous evidence
        # — unlike the reboot ladder this replaced.
        self._flat_max_unwedge_attempts = int(
            runtime_config.get("flat_frame_max_unwedge_attempts", 3)
        )
        self._flat_unwedge_cooldown = float(
            runtime_config.get("flat_frame_unwedge_cooldown", 300)
        )
        self._flat_notify_interval = float(runtime_config.get("flat_frame_notify_interval", 3600))
        self._last_flat_unwedge = 0.0
        self._flat_consecutive_count = 0
        self._flat_forced_reconnects = 0
        self._flat_nonflat_count = 0
        # A repeated frame is proof of a stopped readout, not ambiguous evidence
        # like uniform pixels, so this ladder may do what the unwedge one must
        # not: reboot. On 2026-09-14 a reboot restored readout and three AEC/AGC
        # rewrites did not.
        self._frozen_strikes = int(runtime_config.get("frozen_frame_strikes", 3))
        self._frozen_max_reboots = int(runtime_config.get("frozen_frame_max_reboots", 3))
        self._frozen_reboot_cooldown = float(
            runtime_config.get("frozen_frame_reboot_cooldown", 120.0)
        )
        self._frozen_consecutive_count = 0
        self._frozen_unwedges_seen = 0
        self._last_frozen_reboot = 0.0
        self._last_health_gray = None

    def _frame_is_frozen(self, gray) -> bool:
        """Is this heartbeat's frame a byte-for-byte repeat of the previous one?

        Called on every heartbeat, not only flat ones, so the baseline stays the
        most recent frame — comparing against a stale one would eventually call
        a live sensor frozen.
        """
        frozen = frames_are_identical(self._last_health_gray, gray)
        self._last_health_gray = gray
        return frozen

    def _note_flat_frame(self, current_time: float, frozen: bool = False) -> None:
        """Track a uniform frame and route it to the remedy its cause needs.

        ``frozen`` — the frame repeated the previous one exactly — narrows the
        cause but does not settle it. A uniformly clipped frame encodes to
        identical JPEG bytes with the sensor reading out perfectly well:
        measured on 2026-09-15, a camera held in the firmware's NIGHT profile
        (AGC off, because the enclosure seals the lux sensor) served
        min=max=40, std=0.00, byte-identical — and one exposure write turned it
        into std=14.5, min=0, max=188. So the cheap rewrite is spent first, and
        only frames that stay identical *after* it say the readout has stopped.

        Without it, uniform pixels still cannot tell darkness from a fault, so
        that path never reboots. Rewriting the exposure registers costs two HTTP
        writes, no downtime, and is a no-op on a genuinely dark but healthy
        scene — which is what makes it safe on evidence that ambiguous.
        """
        if self.shared_state.pop("unwedge_write_failed", False):
            # The write never reached the camera, so it says nothing about the
            # exposure loop. Give the attempt back and report the real problem.
            self.flat_state.refund_unwedge()
            if self.flat_state.should_notify(
                "unwedge_failed", current_time, self._flat_notify_interval
            ):
                self.notifier.send_telegram(
                    self._telegram_message(
                        "Camera image has almost no detail and the exposure rewrite "
                        "could not be applied — the camera is not answering on port 80. "
                        "(rate-limited alert)"
                    ),
                    bypass_cooldown=True,
                )

        self._flat_consecutive_count += 1
        self._flat_nonflat_count = 0
        self._frozen_consecutive_count = (
            self._frozen_consecutive_count + 1 if frozen else 0
        )
        # Ignore brief low-detail blips before opening an episode.
        if self._flat_consecutive_count == self._flat_reconnect_strikes:
            self.flat_state.mark_active()

        # Escalate to a reboot only once the exposure rewrite has actually been
        # spent and the bytes still have not moved. Two HTTP writes and no
        # downtime is the cheaper half of the discriminator; ordering it after
        # the reboot cost a real camera three reboots and a pointless request
        # to pull the plug.
        if (
            self._frozen_consecutive_count >= self._frozen_strikes
            and self._frozen_unwedges_seen > 0
        ):
            self.flat_state.mark_active()
            self._note_frozen_sensor(current_time)
            return

        if self._flat_consecutive_count < self._flat_reconnect_strikes:
            return

        action = aec_unwedge_action(
            self._flat_consecutive_count,
            self._flat_reconnect_strikes,
            self.flat_state.unwedge_count(),
            self._flat_max_unwedge_attempts,
            current_time,
            self._last_flat_unwedge,
            self._flat_unwedge_cooldown,
        )

        if action == "unwedge":
            attempts = self.flat_state.record_unwedge()
            self._last_flat_unwedge = current_time
            logging.warning(
                f"{self.log_prefix} Image has had no detail for "
                f"{self._flat_consecutive_count} checks — rewriting AEC/AGC "
                f"({attempts}/{self._flat_max_unwedge_attempts})"
            )
            self.shared_state["unwedge_camera"] = True
            # Restart the frozen run: only repeats measured AFTER an exposure
            # change are evidence that the readout itself has stopped.
            self._frozen_consecutive_count = 0
            self._frozen_unwedges_seen += 1
            if self.flat_state.should_notify(
                "flat_alert", current_time, self._flat_notify_interval
            ):
                self.notifier.send_telegram(
                    self._telegram_message(
                        "Camera image has almost no detail. Rewriting the exposure "
                        "registers (AEC/AGC) to clear a possible wedge — no reboot, "
                        "no downtime. (rate-limited alert)"
                    ),
                    bypass_cooldown=True,
                )
        elif action == "giveup" and self.flat_state.set_gaveup():
            logging.critical(
                f"{self.log_prefix} Image still has no detail after "
                f"{self._flat_max_unwedge_attempts} AEC/AGC rewrites — "
                "the exposure loop is not the cause"
            )
            self.notifier.send_telegram(
                self._telegram_message(
                    "Camera image still has no detail after repeated exposure "
                    "rewrites. The sensor is still producing new frames, so the "
                    "readout has not stopped — what is left is the scene "
                    "itself: a genuinely dark or featureless view, or a "
                    "blocked lens."
                ),
                bypass_cooldown=True,
            )

    def _note_frozen_sensor(self, current_time: float) -> None:
        """A repeated frame means the readout stopped; reboot to restart it.

        Unlike uniform pixels this is not ambiguous, which is what allows a
        reboot here after commit 099fa12 removed reboots from the flat path.
        The budget is persisted so a crash-looping A12 cannot reboot the camera
        all night.
        """
        action = flat_recovery_action(
            self._frozen_consecutive_count,
            self._frozen_strikes,
            0,
            self.flat_state.reboot_count(),
            0,
            self._frozen_max_reboots,
            current_time,
            self._last_frozen_reboot,
            self._frozen_reboot_cooldown,
        )

        if action == "reboot":
            reboots_used = self.flat_state.record_reboot()
            self._last_frozen_reboot = current_time
            logging.warning(
                f"{self.log_prefix} Camera has repeated the same frame for "
                f"{self._frozen_consecutive_count} checks — the sensor stopped "
                f"reading out; rebooting camera over LAN "
                f"({reboots_used}/{self._frozen_max_reboots})"
            )
            self.shared_state["reboot_camera"] = True
            # A hung sensor keeps serving valid MJPEG — uniform frames decode
            # fine — so the stream never ends on its own, and __main__ only
            # drains `reboot_camera` after `process_stream()` returns. Without
            # this the budget is spent and the camera is never rebooted.
            self.shared_state["force_stream_reconnect"] = True
            if self.flat_state.should_notify(
                "frozen_alert", current_time, self._flat_notify_interval
            ):
                self.notifier.send_telegram(
                    self._telegram_message(
                        "Camera is sending the same frame over and over — the "
                        "sensor has stopped reading out (this is not darkness). "
                        "Rebooting it over the LAN. (rate-limited alert)"
                    ),
                    bypass_cooldown=True,
                )
        elif action == "giveup" and self.flat_state.set_gaveup("gaveup_frozen"):
            logging.critical(
                f"{self.log_prefix} Sensor still repeating the same frame after "
                f"{self._frozen_max_reboots} reboots — needs a power cycle"
            )
            self.notifier.send_telegram(
                self._telegram_message(
                    "Camera sensor is still repeating the same frame after "
                    f"{self._frozen_max_reboots} reboots. A soft restart cannot "
                    "clear it — the camera needs a physical power cycle."
                ),
                bypass_cooldown=True,
            )

    def _flat_ladder_note_nonflat(self, current_time: float) -> None:
        """End the low-detail episode only after sustained textured images."""
        self._flat_nonflat_count += 1
        if self._flat_nonflat_count < self._flat_healthy_required:
            return

        self._flat_forced_reconnects = 0

        if self.flat_state.clear() and self.flat_state.should_notify(
            "recovered", current_time, self._flat_notify_interval
        ):
            self.notifier.send_telegram(
                self._telegram_message("Stream recovered — frames are healthy again."),
                bypass_cooldown=True,
            )

    def _note_stream_frame_health(self, healthy: bool, now: float) -> None:
        """Only sustained healthy images can end a stream-freeze episode."""
        if not healthy:
            self._freeze_healthy_frames = 0
            return
        self._freeze_healthy_frames += 1
        if (self._freeze_healthy_frames < self._flat_healthy_required
                or now - self._last_freeze_time < self._freeze_healthy_gap):
            return
        self._freeze_consecutive_count = 0
        if self.freeze_state.clear() and self.freeze_state.should_notify(
            "recovered", now, self._freeze_notify_interval
        ):
            self.notifier.send_telegram(
                self._telegram_message("Stream stable again; sustained healthy frames confirmed."),
                bypass_cooldown=True,
            )

    def note_stream_freeze(self, reason: str) -> None:
        """Register a "frozen"/"stream_ended" stream break; reboot the camera
        over LAN if they keep recurring without a healthy gap between them.

        Called once per break from the __main__ reconnect loop, after the
        stream teardown already happened — so unlike the flat-frame ladder
        this never itself tears down a live connection, it only escalates to
        ``shared_state["reboot_camera"]`` for __main__ to act on before its
        next ``get_stream()`` call.
        """
        now = time.time()
        if now - self._last_freeze_time >= self._freeze_healthy_gap:
            self._freeze_consecutive_count = 0
        self._freeze_healthy_frames = 0
        self._last_freeze_time = now
        self._freeze_consecutive_count += 1

        if self._freeze_consecutive_count == self._freeze_reboot_after:
            self.freeze_state.mark_active()

        action = flat_recovery_action(
            self._freeze_consecutive_count, self._freeze_reboot_after,
            0, self.freeze_state.reboot_count(), 0, self._freeze_max_reboots,
            now, self._last_freeze_action, self._freeze_action_cooldown,
        )
        if action == "reboot":
            reboots_used = self.freeze_state.record_reboot()
            logging.critical(
                f"{self.log_prefix} Stream froze {self._freeze_consecutive_count}x "
                f"({reason}) without a healthy gap — rebooting camera over LAN "
                f"({reboots_used}/{self._freeze_max_reboots})"
            )
            self.shared_state["reboot_camera"] = True
            self._last_freeze_action = now
            self._freeze_consecutive_count = 0
            if self.freeze_state.should_notify("freeze_reboot", now, self._freeze_notify_interval):
                self.notifier.send_telegram(
                    self._telegram_message(
                        "Camera stream keeps freezing — rebooting it over the LAN"
                        " to recover. (rate-limited alert)"
                    ),
                    bypass_cooldown=True,
                )
        elif action == "giveup" and self.freeze_state.set_gaveup():
            logging.critical(
                f"{self.log_prefix} Stream freezes survived {self._freeze_max_reboots} "
                "camera reboots — giving up, hardware needs a manual look"
            )
            self.notifier.send_telegram(
                self._telegram_message(
                    "Camera keeps freezing even after repeated reboots — needs a manual look."
                ),
                bypass_cooldown=True,
            )

    def process_frame(self, frame) -> None:
        """Main per-frame processing callback."""
        if not self.running:
            return

        if self.shared_state.pop("send_recovery_snapshot", False):
            self._send_recovery_snapshot(frame)

        self.frame_count += 1
        self.shared_state["last_frame"] = time.time()

        current_time = time.time()

        # Heartbeat logging
        if current_time - self.last_heartbeat > self.heartbeat_interval:
            elapsed = current_time - self.last_heartbeat
            fps = self.frame_count / elapsed if elapsed > 0 else 0
            if fps > 1:
                self.stream_fps = round(fps)
            rssi = self.status_monitor.current_rssi if self.status_monitor else 0
            pipeline_state = self.shared_state.get("pipeline_state", "unknown")
            decode_fps = int(self.shared_state.get("stream_decode_fps", 0))
            logging.info(
                f"{self.log_prefix} Stream active | state={pipeline_state} | FPS: {fps:.1f} "
                f"| target_decode_fps={decode_fps} | Frames: {self.frame_count} "
                f"| buffer={len(self.frame_buffer)} | queue={self.notification_queue.qsize()} "
                f"| RSSI: {rssi}dBm"
            )
            self.mqtt_client.publish("camera/status/pipeline_state", pipeline_state, retain=True)
            self.mqtt_client.publish("camera/status/stream_fps", f"{fps:.1f}")
            self.mqtt_client.publish("camera/status/decode_fps_target", str(decode_fps), retain=True)
            self.mqtt_client.publish("camera/status/frame_buffer_len", str(len(self.frame_buffer)))
            self.mqtt_client.publish("camera/status/notification_queue", str(self.notification_queue.qsize()))
            self.last_heartbeat = current_time
            self.frame_count = 0

            # Observe image quality independently of camera-setting callbacks.
            gray = cv2.cvtColor(cv2.resize(frame, (160, 120)), cv2.COLOR_BGR2GRAY)
            brightness = float(np.mean(gray))
            flatness = float(np.std(gray))
            # Every heartbeat, not only the flat ones, so the baseline this
            # compares against is always the previous frame.
            frozen = self._frame_is_frozen(gray)
            self.shared_state["last_frame_brightness"] = brightness
            frame_fault = classify_frame_health(
                brightness, flatness, self._dark_frame_threshold, self._flat_frame_std_threshold
            )
            self._note_stream_frame_health(frame_fault is None, current_time)
            if frame_fault:
                self._dark_consecutive_count += 1
                logging.warning(
                    f"{self.log_prefix} {frame_fault.capitalize()} frame detected"
                    f" (brightness={brightness:.1f}, std={flatness:.1f},"
                    f" strike {self._dark_consecutive_count}/{self._dark_consecutive_required})"
                )
                # Both black and gray uniform frames can be legitimate low-light
                # images. Report their appearance without diagnosing a hang.
                if flatness < self._flat_frame_std_threshold:
                    self._note_flat_frame(current_time, frozen=frozen)
                else:
                    # Texture ends the low-detail episode even on a dark night.
                    self._flat_consecutive_count = 0
                    self._flat_ladder_note_nonflat(current_time)
            else:
                self._dark_consecutive_count = 0
                self._flat_consecutive_count = 0
                self._flat_ladder_note_nonflat(current_time)

        # Motion detection
        # Primary: OpenCV frame differencing (disabled when motion.threshold=0)
        motion_enabled = self.runtime_config.get("motion.threshold", 50) > 0
        motion_detected = self.detector.detect_motion(frame) if motion_enabled else False
        # Secondary: ESP32 firmware motion signal received via MQTT (esp32cam/<device>/motion).
        # Uses a 3-second sticky window — camera publishes OFF every 500ms between checks,
        # so a boolean flag would reset before A12 processes the next frame.
        esp32_motion_window = 3.0
        esp32_motion_detected = (
            time.time() - float(self.shared_state.get("last_esp32_motion", 0.0)) < esp32_motion_window
        )
        if esp32_motion_detected:
            motion_detected = True

        if motion_detected:
            self.mqtt_client.publish("motion", "ON")
            with self._counter_lock:
                self.motion_frame_counter += 1
                counter_val = self.motion_frame_counter
        else:
            self.mqtt_client.publish("motion", "OFF")
            counter_val = 0

        # Decide whether to run YOLO
        run_yolo = False
        yolo_reason = ""
        trigger_source = ""

        if self.force_yolo_event.is_set():
            source = self.shared_state.get("external_yolo_source", "external_trigger")
            logging.info(f"Forced YOLO check ({source})!")
            run_yolo = True
            yolo_reason = "external_trigger"
            trigger_source = str(source)
            self.last_external_yolo = current_time
            self.force_yolo_event.clear()
        elif current_time < float(self.shared_state.get("external_yolo_until", 0.0)):
            if current_time - self.last_external_yolo >= self.external_yolo_interval:
                source = self.shared_state.get("external_yolo_source", "external_trigger")
                logging.info(
                    "External trigger YOLO window active "
                    f"({source}, interval={self.external_yolo_interval:.1f}s)"
                )
                run_yolo = True
                yolo_reason = "external_trigger_window"
                trigger_source = str(source)
                self.last_external_yolo = current_time
        elif motion_detected and counter_val % self.yolo_check_interval == 0:
            run_yolo = True
            yolo_reason = "motion_detected"
            trigger_source = yolo_reason
        elif (current_time - self.last_periodic_yolo) > self.periodic_yolo_interval:
            logging.info(f"Periodic YOLO check (no motion for {self.periodic_yolo_interval}s)")
            run_yolo = True
            yolo_reason = "periodic"
            trigger_source = yolo_reason
            self.last_periodic_yolo = current_time

        self._buffer_clip_frame_if_needed(frame, current_time)
        self._handle_pir_recording(frame, current_time)

        if not run_yolo:
            return

        # YOLO inference
        all_detections = self.detector.detect_objects(frame)

        person_candidates = [(label, confidence) for label, confidence in all_detections if label == "person"]
        is_pir_triggered = yolo_reason.startswith("external_trigger")
        detection_profile = "pir" if is_pir_triggered else "camera"
        notify_confidence = (
            self.pir_person_notify_confidence
            if is_pir_triggered
            else self.person_notify_confidence
        )
        confirmations_required = (
            self.pir_person_confirmations_required
            if is_pir_triggered
            else self.person_confirmations_required
        )
        person_detections = [
            (label, confidence)
            for label, confidence in person_candidates
            if confidence >= notify_confidence
        ]
        animal_detections = [
            (label, confidence) for label, confidence in all_detections if label != "person"
        ]

        person_found = len(person_detections) > 0
        max_person_confidence = max((c for _label, c in person_candidates), default=0.0)
        candidate_label = "person" if person_candidates else ""
        yolo_confidence_threshold = float(
            self.runtime_config.get("yolo.confidence_threshold", notify_confidence)
        )
        audit_context = {
            "trigger_source": trigger_source or yolo_reason,
            "backend": str(getattr(self.detector, "last_backend", "local")),
            "candidate_label": candidate_label,
            "candidate_confidence": max_person_confidence if person_candidates else None,
            "yolo_confidence_threshold": yolo_confidence_threshold,
            "notify_confidence_threshold": float(notify_confidence),
            "confirmations_required": confirmations_required,
            "notify_threshold": self.event_notify_threshold,
            "local_record_threshold": self.event_local_record_threshold,
        }
        logging.info(
            "YOLO calibration: profile=%s reason=%s candidate=%.3f notify_threshold=%.3f accepted=%s",
            detection_profile, yolo_reason, max_person_confidence, notify_confidence, person_found,
        )
        if person_candidates and not person_found:
            max_conf = max(c for _l, c in person_candidates)
            logging.info(
                "Person candidate below notify threshold: "
                f"{max_conf:.2f} < {notify_confidence:.2f} "
                f"(profile={detection_profile}, reason={yolo_reason})"
            )

        if person_found:
            candidate_box = getattr(self.detector, "last_person_box", None)
            prior_box = self.person_confirmation_boxes[detection_profile]
            prior_seen = self.person_confirmation_seen_at[detection_profile]
            overlap = box_iou(prior_box, candidate_box)
            if self.person_confirmation_streaks[detection_profile] and (
                current_time - prior_seen > self.person_confirmation_max_gap
                or (overlap is not None and overlap < self.person_confirmation_iou)
            ):
                logging.info(
                    "Person confirmation reset: gap=%.1fs iou=%s",
                    current_time - prior_seen,
                    "none" if overlap is None else f"{overlap:.2f}",
                )
                self.person_confirmation_streaks[detection_profile] = 0
            self.person_confirmation_streaks[detection_profile] += 1
            self.person_confirmation_boxes[detection_profile] = candidate_box
            self.person_confirmation_seen_at[detection_profile] = current_time
            confirmation_streak = self.person_confirmation_streaks[detection_profile]
            other_profile = "camera" if detection_profile == "pir" else "pir"
            self.person_confirmation_streaks[other_profile] = 0
            if confirmation_streak < confirmations_required:
                max_conf = max(c for _l, c in person_detections)
                logging.info(
                    "Person candidate held for confirmation "
                    f"({confirmation_streak}/{confirmations_required}, "
                    f"confidence={max_conf:.2f}, profile={detection_profile}, "
                    f"reason={yolo_reason})"
                )
                person_found = False
                person_detections = []
        else:
            self.person_confirmation_streaks[detection_profile] = 0
            self.person_confirmation_boxes[detection_profile] = None
            self.person_confirmation_seen_at[detection_profile] = 0.0
        if not person_candidates:
            self._log_decision_audit(
                audit_context, 0, None, None, None, "no_person_candidate",
                frame=frame,
            )
        elif not person_detections:
            outcome = (
                "below_notify_confidence"
                if max_person_confidence < notify_confidence
                else "awaiting_confirmation"
            )
            self._log_decision_audit(
                audit_context,
                self.person_confirmation_streaks[detection_profile],
                None,
                None,
                None,
                outcome,
                frame=frame,
            )


        self.stats.record_motion_event(esp32_motion_detected, motion_detected, person_found)
        self.mqtt_client.publish("person", "ON" if person_found else "OFF")

        if self.runtime_config.get("debug_detection", False):
            logging.debug(f"YOLO: person={person_found}, detections={len(person_detections)}")

        if person_detections:
            self.shared_state["last_person_activity"] = current_time
            self.shared_state["last_event_activity"] = current_time
            confirmation_streak = self.person_confirmation_streaks[detection_profile]
            self._handle_person_detections(
                frame,
                person_detections,
                yolo_reason,
                detection_profile,
                confirmation_streak,
                audit_context,
            )

        if animal_detections:
            self._handle_animal_detections(frame, animal_detections)

    def _log_decision_audit(
        self,
        audit_context: dict,
        confirmation_streak: int,
        sensor_confirmed: bool | None,
        active_sensors: list[str] | None,
        event_score: int | None,
        decision_outcome: str,
        frame=None,
    ) -> int | None:
        """Add the final policy state to one YOLO audit record.

        Returns the audit row id so the caller can attach the media it goes on
        to save.
        """
        audit = dict(audit_context)
        audit.update(
            confirmation_streak=confirmation_streak,
            sensor_confirmed=sensor_confirmed,
            active_sensors=active_sensors,
            event_score=event_score,
            decision_outcome=decision_outcome,
        )
        audit_id = self.db.log_decision_audit(**audit)
        if frame is None:
            return audit_id
        if audit.get("candidate_label"):
            if decision_outcome not in OUTCOMES_WITH_OWN_MEDIA:
                self._save_candidate_snapshot(
                    frame, audit_id, audit.get("candidate_confidence"), decision_outcome
                )
        elif decision_outcome == "no_person_candidate":
            self._save_miss_snapshot(
                frame, audit_id, audit.get("trigger_source"))
        return audit_id

    def _save_candidate_snapshot(
        self, frame, audit_id: int | None, confidence: float | None, outcome: str
    ) -> str | None:
        """Keep the frame behind a person-candidate audit row.

        Rejected and unconfirmed candidates otherwise leave no image, so their
        audit rows can never be verified against ground truth — the one thing
        threshold calibration and dataset mining need. Files live in
        screenshots/candidates/ and age out with the decision audit, not with
        the 2-day media sweep.
        """
        if not self.candidate_snapshot_enabled:
            return None
        now = time.time()
        if now - self._last_candidate_snapshot < self.candidate_snapshot_min_interval:
            return None
        try:
            folder = os.path.join(self.screenshot_folder, "candidates")
            os.makedirs(folder, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            conf = f"{confidence:.2f}" if confidence is not None else "na"
            name = f"cand_{stamp}_a{audit_id if audit_id is not None else 'x'}_{conf}_{outcome}.jpg"
            path = os.path.join(folder, name)
            if not cv2.imwrite(path, frame):
                return None
            self._last_candidate_snapshot = now
            return path
        except Exception as e:
            logging.warning(f"Candidate snapshot failed: {e}")
            return None

    def _save_miss_snapshot(
        self, frame, audit_id: int | None, trigger_source: str | None
    ) -> str | None:
        """Keep the frame behind a sensor-triggered "nobody here" decision.

        A sensor fired and YOLO found no candidate at all. That is either a real
        miss — the failure this project cares most about — or a cat, a branch or
        rain, and the two are indistinguishable without the image. These rows
        carry no candidate_label, so the candidate gate above never covers them,
        which left the largest population in the audit as the only one with no
        evidence at all. Files live in screenshots/misses/ and age out with the
        decision audit.
        """
        if not self.miss_snapshot_enabled:
            return None
        if not trigger_source or trigger_source in UNTRIGGERED_SOURCES:
            return None
        now = time.time()
        if now - self._last_miss_snapshot < self.miss_snapshot_min_interval:
            return None
        try:
            folder = os.path.join(self.screenshot_folder, "misses")
            os.makedirs(folder, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            name = f"miss_{stamp}_a{audit_id if audit_id is not None else 'x'}.jpg"
            path = os.path.join(folder, name)
            if not cv2.imwrite(path, frame):
                return None
            self._last_miss_snapshot = now
            return path
        except Exception as e:
            logging.warning(f"Miss snapshot failed: {e}")
            return None

    def _score_event(
        self,
        confidence: float,
        sensor_confirmed: bool,
        is_pir_triggered: bool,
        confirmation_streak: int,
    ) -> int:
        """Score a person event so PIR-assisted detections can be faster than camera-only ones."""
        if not self.event_scoring_enabled:
            return self.event_notify_threshold

        yolo_score = int(self.runtime_config.get("event_scoring.yolo_person_score", 55))
        sensor_score = int(self.runtime_config.get("event_scoring.sensor_active_score", 45))
        pir_score = int(self.runtime_config.get("event_scoring.pir_trigger_score", 15))
        confirmation_score = int(self.runtime_config.get("event_scoring.confirmation_score", 15))
        confidence_bonus_score = int(
            self.runtime_config.get("event_scoring.confidence_bonus_score", 25)
        )

        score = yolo_score
        if sensor_confirmed:
            score += sensor_score
        if is_pir_triggered:
            score += pir_score
        if confirmation_streak >= 2:
            score += confirmation_score

        confidence_floor = self.pir_person_notify_confidence if is_pir_triggered else self.person_notify_confidence
        confidence_span = max(0.01, 1.0 - confidence_floor)
        confidence_ratio = max(0.0, min(1.0, (confidence - confidence_floor) / confidence_span))
        score += round(confidence_bonus_score * confidence_ratio)
        return score

    def _local_only_clip_message(self, event_score: int | None, mp4_path: str) -> str:
        """Say WHY a clip stayed local. score=None used to read as a scoring
        failure — but None means no event score was ever computed: the clip came
        from the PIR path, which suppresses Telegram until YOLO confirms a person."""
        if event_score is None:
            return (
                "Telegram suppressed (clip without confirmed person); "
                f"local MP4 saved (path={mp4_path})"
            )
        return (
            "Telegram skipped by event score; local MP4 saved "
            f"(score={event_score}, path={mp4_path})"
        )

    def _should_buffer_clip_frames(self, current_time: float) -> bool:
        """Buffer clips only after PIR/HA activity or while a clip is being completed."""
        if current_time < float(self.shared_state.get("external_yolo_until", 0.0)):
            return True
        if current_time < self.recording_buffer_until:
            return True
        return bool(self.ha_monitor and self.ha_monitor.is_any_sensor_active())

    def _pir_window_active(self, current_time: float) -> bool:
        """Is somebody standing in front of the camera right now?

        `external_yolo_until` is the sticky window opened by a PIR on-edge; the
        live sensor level covers the case where the window has already lapsed
        but the person has not left. The recording buffer is deliberately NOT
        consulted here — it stays open long after the occurrence ends.
        """
        if current_time < float(self.shared_state.get("external_yolo_until", 0.0)):
            return True
        return bool(self.ha_monitor and self.ha_monitor.is_any_sensor_active())

    def _current_face_episode(self, current_time: float) -> FaceEpisode:
        """The episode in progress, starting a fresh one after a quiet gap.

        Without this, two people a minute apart would share a verdict and the
        second one would inherit the first one's suppression.
        """
        if current_time - self._last_face_check_at >= self._face_episode_gap:
            self._face_episode.reset()
        return self._face_episode

    def configure_face_checks(self, face_cfg: dict) -> None:
        """Everything the face check needs to know, in one place.

        Split out of __init__ so tests reach the real wiring instead of
        re-declaring the attribute list by hand — a stub that drifts from
        __init__ passes while production raises AttributeError.
        """
        # The check is only affordable, and only answerable, while the PIR says
        # somebody is standing in the doorway.
        self._face_require_pir_window = bool(face_cfg.get("require_pir_window", True))
        self._face_max_checks = max(1, int(face_cfg.get("max_checks_per_episode", 5)))
        self._face_min_check_interval = max(
            0.0, float(face_cfg.get("min_check_interval_seconds", 0.5))
        )
        self._face_box_margin = max(0.0, float(face_cfg.get("person_box_margin", 0.25)))
        self._face_episode_gap = max(1.0, float(face_cfg.get("episode_gap_seconds", 30.0)))
        self._face_episode = FaceEpisode(face_cfg.get("episode_resident_confirmations", 2))
        self._last_face_check_at = 0.0
        # Tuning aid: keep the exact crop that was checked, named by outcome
        # and score. It is the only way to tell "the face was too small" from
        # "the crop missed" from "the threshold is wrong" — the verdict alone
        # hides all three. Off unless a directory is configured.
        self._face_debug_dir = str(face_cfg.get("debug_crop_dir", "") or "")
        self._face_debug_limit = max(0, int(face_cfg.get("debug_crop_limit", 200)))
        # Seeded from what is already on disk. A plain counter restarted at zero
        # in every process, so each A12 restart granted a fresh quota — and a
        # camera crash loop restarts A12 repeatedly, which is exactly when the
        # directory would run away.
        self._face_debug_written = self._count_face_debug_crops()

    def _count_face_debug_crops(self) -> int:
        if not self._face_debug_dir:
            return 0
        try:
            return len(os.listdir(self._face_debug_dir))
        except OSError:
            return 0

    def _save_face_debug_crop(self, crop, result: FaceResult, when: float) -> None:
        """Best-effort: a debug convenience must never cost a detection."""
        if not self._face_debug_dir or self._face_debug_written >= self._face_debug_limit:
            return
        try:
            os.makedirs(self._face_debug_dir, exist_ok=True)
            path = os.path.join(
                self._face_debug_dir,
                debug_crop_name(when, result, seq=self._face_debug_written),
            )
            if cv2.imwrite(path, crop):
                self._face_debug_written += 1
                if self._face_debug_written == self._face_debug_limit:
                    logging.info(
                        f"Face debug crops reached the limit of "
                        f"{self._face_debug_limit}; no more will be written"
                    )
        except Exception as e:
            logging.debug(f"Could not save face debug crop: {e}")

    def _face_verdict(self, frame) -> FaceResult:
        """Spend at most one face check on this frame, then answer for the episode.

        The check only runs while somebody is actually at the door, on the
        person box rather than the whole frame, a bounded number of times per
        occurrence. The answer returned is always the episode's, never this
        one frame's — a single frame's opinion is what the old code shipped
        and what the aggregation is meant to replace.
        """
        now = time.time()
        episode = self._current_face_episode(now)
        if should_run_face_check(
            enabled=True,
            pir_window_active=self._pir_window_active(now),
            require_pir_window=self._face_require_pir_window,
            have_person_box=self.detector.last_person_box is not None,
            checks_done=episode.checks_done,
            max_checks=self._face_max_checks,
            now=now,
            last_check_at=self._last_face_check_at,
            min_interval=self._face_min_check_interval,
        ):
            # The box is already computed and thrown away today; a face is a
            # few percent of a 640x480 frame.
            crop = crop_person_box(
                frame, self.detector.last_person_box, self._face_box_margin
            )
            check = self.detector.identify_person(crop)
            self._save_face_debug_crop(crop, check, now)
            episode.record(check)
            self._last_face_check_at = now
            self.stats.record_face_attempt(check)
        return episode.verdict()

    def _buffer_clip_frame_if_needed(self, frame, current_time: float) -> None:
        """Keep a rolling pre-event buffer at idle FPS; ramp to clip_fps during active windows.

        Always buffering (even in idle) ensures pre-event frames exist when the first
        PIR trigger fires — without this the 5s pre-buffer would always be empty.
        """
        if self._should_buffer_clip_frames(current_time):
            interval = self.clip_buffer_interval  # full clip FPS during active window
        else:
            idle_fps = max(1, int(self.runtime_config.get("stream_idle_decode_fps", 2)))
            interval = 1.0 / idle_fps

        if current_time - self.last_clip_buffer_sample < interval:
            return

        clip_frame = cv2.resize(frame, self.clip_frame_size)
        self.frame_buffer.append((current_time, clip_frame))
        self.last_clip_buffer_sample = current_time

    def _handle_person_detections(
        self,
        frame,
        detections: list,
        yolo_reason: str,
        detection_profile: str,
        confirmation_streak: int,
        audit_context: dict,
    ) -> None:
        """Process person detections with sensor fusion logic."""
        for label, confidence in detections:
            logging.info(f"Detected: {label} ({confidence:.2f})")

            # Sensor fusion
            security_mode = self.runtime_config.get("security.mode", "MONITOR")
            require_confirmation = self.runtime_config.get("security.require_sensor_confirmation", False)
            sensor_window = self.runtime_config.get("security.sensor_fusion_window_seconds", 10.0)

            sensor_confirmed = False
            active_sensors = []

            if self.ha_monitor:
                current_active_sensors = self.ha_monitor.get_active_sensors()
                recent_active_sensors = self.ha_monitor.get_recently_active_sensors(sensor_window)
                active_sensors = sorted(set(current_active_sensors + recent_active_sensors))
                sensor_confirmed = len(active_sensors) > 0

            # ESP32 firmware motion signal counts as sensor confirmation
            if not sensor_confirmed:
                esp32_motion_age = time.time() - float(self.shared_state.get("last_esp32_motion", 0.0))
                if esp32_motion_age < sensor_window:
                    sensor_confirmed = True
                    active_sensors.append("esp32_motion")

            # Event classification
            event_classification = "CAMERA_ONLY"
            event_priority = "normal"
            should_notify = True
            alarm_triggered = False
            is_pir_triggered = detection_profile == "pir"
            event_score = self._score_event(
                float(confidence),
                sensor_confirmed,
                is_pir_triggered,
                confirmation_streak,
            )

            if security_mode == "SECURITY":
                if sensor_confirmed:
                    event_classification = "CONFIRMED_ALARM"
                    event_priority = "critical"
                    alarm_triggered = True
                    logging.warning(
                        f"CONFIRMED ALARM: Person + {len(active_sensors)} sensor(s)"
                    )
                    self.mqtt_client.publish("camera/alarm/trigger", "ON")
                else:
                    event_classification = "UNCONFIRMED_CAMERA"
                    event_priority = "low"
                    if require_confirmation:
                        logging.info(f"UNCONFIRMED: Person detected, no sensor ({sensor_window}s)")
                        should_notify = False
                        self.db.log_event("unconfirmed_detection", label, float(confidence))
                    else:
                        logging.info("Person detected (no sensor confirmation - allowed)")

            elif security_mode == "MONITOR":
                event_classification = "MONITOR_MODE"
                if sensor_confirmed:
                    logging.info("Person detected + Sensor active (MONITOR mode)")

            else:  # TEST mode
                event_classification = "TEST_MODE"
                event_priority = "debug"
                logging.info(f"TEST MODE: Person={label}, Sensors={sensor_confirmed}")

            self.mqtt_client.publish("camera/detection/classification", event_classification)
            self.mqtt_client.publish("camera/detection/priority", event_priority)
            self.mqtt_client.publish(
                "camera/detection/sensor_confirmed",
                "true" if sensor_confirmed else "false",
            )
            self.mqtt_client.publish("camera/detection/score", str(event_score))

            if self.require_sensor_for_recording and not sensor_confirmed:
                self._log_decision_audit(
                    audit_context, confirmation_streak, sensor_confirmed,
                    active_sensors, event_score, "not_recorded_sensor_required",
                    frame=frame,
                )
                logging.info(
                    "Person event ignored for recording: no PIR/HA sensor confirmation "
                    f"(profile={detection_profile}, reason={yolo_reason}, score={event_score})"
                )
                continue

            should_record_locally = event_score >= self.event_local_record_threshold
            if event_score < self.event_notify_threshold:
                should_notify = False
                logging.info(
                    "Person event kept local only "
                    f"(score={event_score}, notify_threshold={self.event_notify_threshold}, "
                    f"profile={detection_profile}, reason={yolo_reason})"
                )

            if not should_record_locally:
                self._log_decision_audit(
                    audit_context, confirmation_streak, sensor_confirmed,
                    active_sensors, event_score, "not_recorded_below_local_threshold",
                    frame=frame,
                )
                logging.info(
                    "Person event ignored below local record threshold "
                    f"(score={event_score}, threshold={self.event_local_record_threshold})"
                )
                continue

            # Cooldown check
            if label in self.last_save_time:
                if (time.time() - self.last_save_time[label]) < self.cooldown_seconds:
                    self._log_decision_audit(
                        audit_context, confirmation_streak, sensor_confirmed,
                        active_sensors, event_score, "suppressed_by_cooldown",
                        frame=frame,
                    )
                    continue

            audit_id = self._log_decision_audit(
                audit_context, confirmation_streak, sensor_confirmed, active_sensors, event_score,
                "recorded_and_notified" if should_notify else "recorded_local_only",
                frame=frame,
            )

            self.stats.record_detection(label)
            self.db.log_event("detection", label, float(confidence))

            # Face recognition — Groq vision (primary) or dlib fallback
            person_name = ""
            skip_telegram = False

            if self.groq_vision is not None:
                gname, gconf, gdecision = self.groq_vision.identify(frame)
                if gdecision == "cooldown":
                    pass  # rate limit — skip silently
                else:
                    self.db.log_event("face", gname or "unknown", gconf)
                    self.mqtt_client.publish("face", gname or "unknown")
                    logging.info(f"{self.log_prefix} Groq face: {gname} ({gconf:.2f}) → {gdecision}")

                if gdecision == "auto":
                    person_name = gname
                    skip_telegram = True
                    self._known_person_until = time.time() + 60.0
                    logging.info(f"{self.log_prefix} Known person '{gname}' — Telegram skipped, unlock queued")
                    self._trigger_nuki_unlock(gname)
                elif gdecision == "confirm":
                    person_name = gname
                    # notifier will send Telegram with unlock button — handled in notify call below

            elif self.runtime_config.get("face_recognition", {}).get("enabled", False):
                face = self._face_verdict(frame)
                # Only a resident may put a name into the caption. Appending the
                # raw result used to produce "Person detected (Video) (No face)".
                person_name = notification_name(face)
                face_label = face.name if face.is_resident else face.outcome.value
                self.db.log_event("face", face_label, 1.0 if face.is_resident else 0.0)
                self.mqtt_client.publish("face", face_label)

                whitelist = self.runtime_config.get(
                    "face_recognition.whitelisted_names", []
                )
                if face.is_resident and face.name in whitelist:
                    skip_telegram = True
                    logging.info(f"Known person: {face.name} - Telegram skipped")

            # Save & notify
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            label_folder = os.path.join(self.screenshot_folder, label)
            os.makedirs(label_folder, exist_ok=True)
            self.last_save_time[label] = time.time()

            # Always save JPG locally
            jpg_path = os.path.join(label_folder, self._media_name(label, timestamp, ".jpg"))
            cv2.imwrite(jpg_path, frame)
            self.db.log_event("media", "jpg", 0.0, jpg_path)
            # Close the audit → media link while both are still in scope.
            self.db.set_decision_audit_media(audit_id, jpg_path)

            if skip_telegram:
                continue

            display_label = label
            if alarm_triggered:
                display_label = "ALARM! " + label.upper()

            self._queue_notification(
                display_label,
                timestamp,
                person_name,
                label_folder,
                send_telegram=should_notify,
                event_score=event_score,
            )

    def _handle_pir_recording(self, frame, current_time: float) -> None:
        """Create a clip for each new PIR/HA trigger even when YOLO finds no person."""
        if not self.pir_recording_enabled:
            return

        sensor_activity = float(self.shared_state.get("last_sensor_activity", 0.0))
        if sensor_activity <= 0 or sensor_activity <= self.last_pir_sensor_activity_recorded:
            return

        external_window_active = current_time < float(
            self.shared_state.get("external_yolo_until", 0.0)
        )
        sensor_active = bool(self.ha_monitor and self.ha_monitor.is_any_sensor_active())
        if not external_window_active and not sensor_active:
            return

        if current_time - self.last_pir_record_time < self.pir_recording_cooldown:
            logging.info(
                "PIR recording cooldown active "
                f"({current_time - self.last_pir_record_time:.0f}s / "
                f"{self.pir_recording_cooldown}s)"
            )
            self.last_pir_sensor_activity_recorded = sensor_activity
            return

        label = self.pir_recording_label
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        label_folder = os.path.join(self.screenshot_folder, label)
        os.makedirs(label_folder, exist_ok=True)

        jpg_path = os.path.join(label_folder, self._media_name(label, timestamp, ".jpg"))
        cv2.imwrite(jpg_path, frame)
        self.db.log_event("detection", label, 1.0)
        self.db.log_event("media", "jpg", 0.0, jpg_path)
        self.stats.record_detection(label)

        self.last_pir_record_time = current_time
        self.last_pir_sensor_activity_recorded = sensor_activity
        self.shared_state["last_event_activity"] = current_time

        known_person_active = current_time < self._known_person_until
        logging.info("PIR trigger queued recording without YOLO confirmation")
        # When require_yolo_for_telegram is True, suppress Telegram from the PIR path.
        # The existing external_yolo_until window is already open; if YOLO confirms a person,
        # _handle_person_detections will send Telegram through its own path.
        pir_send_telegram = (
            self.pir_recording_send_telegram
            and not known_person_active
            and not self.pir_recording_require_yolo
        )
        self._queue_notification(
            label,
            timestamp,
            "",
            label_folder,
            send_telegram=pir_send_telegram,
            bypass_telegram_cooldown=self.pir_recording_bypass_cooldown,
            event_score=None,
        )

    def _handle_animal_detections(self, frame, detections: list) -> None:
        """Process animal detections with security mode filtering."""
        security_mode = self.runtime_config.get("security.mode", "MONITOR")
        detect_animals = self.runtime_config.get("security.animal_detection", True)

        if not detect_animals:
            return

        for label, confidence in detections:
            if security_mode == "SECURITY":
                logging.info(f"Animal ignored in SECURITY mode: {label} ({confidence:.2f})")
                continue

            if label in self.last_save_time:
                if (time.time() - self.last_save_time[label]) < self.cooldown_seconds:
                    continue

            logging.info(f"Animal Detected: {label} ({confidence:.2f})")
            self.mqtt_client.publish("camera/detection/animal", label)
            self.db.log_event("detection", label, float(confidence))
            self.stats.record_detection(label)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            label_folder = os.path.join(self.screenshot_folder, label)
            os.makedirs(label_folder, exist_ok=True)
            self.last_save_time[label] = time.time()

            jpg_path = os.path.join(label_folder, self._media_name(label, timestamp, ".jpg"))
            cv2.imwrite(jpg_path, frame)
            self.db.log_event("media", "jpg", 0.0, jpg_path)

            self._queue_notification(
                label,
                timestamp,
                "",
                label_folder,
                send_telegram=True,
                event_score=None,
            )

    def _queue_notification(
        self,
        label: str,
        timestamp: str,
        person_name: str,
        label_folder: str,
        send_telegram: bool = True,
        bypass_telegram_cooldown: bool = False,
        event_score: int | None = None,
    ) -> None:
        """Queue async notification task for GIF/MP4 creation."""
        audio_data = None
        if self.audio_monitor and self.audio_monitor.running:
            audio_data = self.audio_monitor.get_audio_data()

        queued_at = time.time()
        max_post_seconds = self._effective_max_post_seconds(self.post_buffer_seconds)
        self.recording_buffer_until = max(
            self.recording_buffer_until,
            queued_at + max_post_seconds + 2,
        )
        self.shared_state["recording_buffer_until"] = self.recording_buffer_until
        pre_cutoff = queued_at - self.clip_pre_seconds
        pre_frames = [
            buffered_frame
            for frame_ts, buffered_frame in list(self.frame_buffer)
            if frame_ts >= pre_cutoff and frame_ts <= queued_at
        ]

        task = {
            "label": label,
            "pre_frames": pre_frames,
            "frame_buffer_ref": self.frame_buffer,
            "post_seconds": self.post_buffer_seconds,
            "queued_at": queued_at,
            "stream_fps": self.stream_fps,
            "audio_data": audio_data,
            "timestamp": timestamp,
            "person_name": person_name,
            "label_folder": label_folder,
            "send_telegram": send_telegram,
            "bypass_telegram_cooldown": bypass_telegram_cooldown,
            "event_score": event_score,
        }
        try:
            self.notification_queue.put_nowait(task)
        except queue.Full:
            logging.warning(
                f"{self.log_prefix} Notification queue full; dropping {label} event "
                f"at {timestamp}"
            )

    def _retention_days_for(self, top_folder: str) -> float:
        """Learning data outlives the default media sweep: person clips follow
        their own retention, candidate snapshots follow the decision audit's.

        The audit keeps its own raw value because EventDB.prune_decision_audit
        needs 0 to mean "do not prune"; retention_days maps it to infinity for
        the file sweep so the images survive with the rows.
        """
        if top_folder == "person":
            return self.person_media_retention_days
        if top_folder in ("candidates", "misses"):
            return retention_days(self.decision_audit_retention_days)
        return self.cleanup_max_age_days

    def _cleanup_old_media(self) -> None:
        """Remove media files past their folder's retention from screenshots."""
        now = time.time()
        removed = 0
        try:
            for root, _dirs, files in os.walk(self.screenshot_folder):
                rel = os.path.relpath(root, self.screenshot_folder)
                top = "" if rel == "." else rel.split(os.sep)[0]
                cutoff = now - self._retention_days_for(top) * 86400
                for fname in files:
                    fpath = os.path.join(root, fname)
                    if os.path.getmtime(fpath) < cutoff:
                        os.remove(fpath)
                        removed += 1
            if removed:
                logging.info(f"Cleanup: removed {removed} media files past retention")
        except Exception as e:
            logging.error(f"Cleanup error: {e}")

    def _run_scheduled_cleanup(self, now: float) -> None:
        if now - self.last_cleanup <= self.cleanup_interval:
            return
        self._cleanup_old_media()
        self.db.prune_decision_audit(self.decision_audit_retention_days)
        self.last_cleanup = now

    def _sample_preview_frames(self, frames: list, seconds: int, fps: int) -> list:
        """Sample a short preview across the whole local clip."""
        if not frames:
            return []
        target_count = max(1, min(len(frames), int(seconds) * int(fps)))
        if target_count >= len(frames):
            return frames
        if target_count == 1:
            return [frames[len(frames) // 2]]
        last = len(frames) - 1
        return [frames[round(i * last / (target_count - 1))] for i in range(target_count)]

    def _effective_max_post_seconds(self, base_post_seconds: int) -> int:
        """Longest post window that still yields a gap-free clip.

        See `_max_retainable_post_seconds` in __init__: the configured maximum is
        capped by how much history the rolling frame buffer actually keeps.
        """
        configured = max(
            base_post_seconds,
            int(self.runtime_config.get("adaptive_clip.max_post_seconds", 60)),
        )
        effective = min(configured, self._max_retainable_post_seconds)
        if effective < configured and not self._logged_post_clamp:
            self._logged_post_clamp = True
            logging.info(
                f"{self.log_prefix} Adaptive clip post window capped at {effective}s "
                f"(configured {configured}s) — the frame buffer only retains "
                f"{self._max_retainable_post_seconds}s. Raise clip_post_seconds to extend it."
            )
        return effective

    def _wait_for_event_tail(self, queued_at: float, base_post_seconds: int) -> float:
        """Wait for post-event frames, extending while PIR/YOLO activity continues."""
        if base_post_seconds <= 0:
            return queued_at

        if not bool(self.runtime_config.get("adaptive_clip.enabled", True)):
            time.sleep(base_post_seconds)
            return queued_at + base_post_seconds

        idle_seconds = max(1, int(self.runtime_config.get("adaptive_clip.idle_seconds", 10)))
        max_post_seconds = self._effective_max_post_seconds(base_post_seconds)
        min_deadline = queued_at + base_post_seconds
        max_deadline = queued_at + max_post_seconds

        while self.running:
            now = time.time()
            if now >= max_deadline:
                logging.info(f"Adaptive clip reached max post window ({max_post_seconds}s)")
                return max_deadline

            last_activity = max(
                float(self.shared_state.get("last_person_activity", 0.0)),
                float(self.shared_state.get("last_sensor_activity", 0.0)),
                min(float(self.shared_state.get("external_yolo_until", 0.0)), now),
            )
            if self.ha_monitor and self.ha_monitor.is_any_sensor_active():
                last_activity = now

            if now >= min_deadline and now - last_activity >= idle_seconds:
                return now

            time.sleep(0.5)

        return time.time()

    def _notification_worker(self) -> None:
        """Background thread to handle media creation and Telegram notifications."""
        logging.info(f"{self.log_prefix} Notification worker started")
        while self.running:
            try:
                task = self.notification_queue.get(timeout=1)
                label = task["label"]
                audio_data = task.get("audio_data")
                timestamp = task["timestamp"]
                person_name = task.get("person_name", "")
                label_folder = task["label_folder"]
                send_telegram = bool(task.get("send_telegram", True))
                bypass_telegram_cooldown = bool(task.get("bypass_telegram_cooldown", False))
                event_score = task.get("event_score")

                # Wait for post-detection frames
                post_seconds = task.get("post_seconds", 0)
                if post_seconds > 0:
                    post_deadline = self._wait_for_event_tail(task.get("queued_at", time.time()), post_seconds)
                    if self.audio_monitor and self.audio_monitor.running:
                        audio_data = self.audio_monitor.get_audio_data()
                else:
                    post_deadline = task.get("queued_at", time.time())

                # Merge pre + post frames by timestamp. This keeps a real
                # event window instead of whatever happens to remain in deque.
                pre_frames = task.get("pre_frames", [])
                buf_ref = task.get("frame_buffer_ref")
                queued_at = task.get("queued_at", time.time())
                post_deadline = post_deadline + 0.5
                buffered = list(buf_ref) if buf_ref is not None else []
                post_frames = [
                    buffered_frame
                    for frame_ts, buffered_frame in buffered
                    if frame_ts > queued_at and frame_ts <= post_deadline
                ]
                frames = pre_frames + post_frames

                # Try MP4 first. Audio is optional; if MP4 fails, keep GIF as
                # the fallback so Telegram still gets a motion preview.
                mp4_created = False
                mp4_path = os.path.join(label_folder, self._media_name(label, timestamp, ".mp4"))
                msg = f"{label.title()} detected (AV Clip)" if audio_data else f"{label.title()} detected (Video)"
                if person_name:
                    msg += f" ({person_name})"

                # fps must match the rate the frames were *sampled* at, not the rate
                # the stream was decoded at. These come from frame_buffer, which
                # _buffer_clip_frame_if_needed fills at clip_fps during an active
                # window. Writing them at stream_fps (~10 while a window is open)
                # played every real event back at roughly double speed; it only
                # looked right when testing idle, where the two rates happen to match.
                if self.notifier.create_mp4(frames, audio_data, mp4_path, fps=self.clip_fps):
                    self.db.log_event("media", "mp4", 0.0, mp4_path)
                    mp4_created = True

                    media_mode = str(
                        self.runtime_config.get("telegram.media_mode", "mp4")
                    ).lower()
                    if not send_telegram:
                        logging.info(self._local_only_clip_message(event_score, mp4_path))
                    elif media_mode == "mp4":
                        self.notifier.send_telegram(
                            self._telegram_message(msg), mp4_path, bypass_cooldown=bypass_telegram_cooldown
                        )
                    elif media_mode == "preview_mp4":
                        preview_path = os.path.join(label_folder, self._media_name(label, timestamp, "_preview.mp4"))
                        preview_seconds = int(self.runtime_config.get("telegram.preview_seconds", 4))
                        preview_fps = int(self.runtime_config.get("telegram.preview_fps", 4))
                        preview_frames = self._sample_preview_frames(
                            frames, preview_seconds, preview_fps
                        )
                        if self.notifier.create_mp4(
                            preview_frames,
                            None,
                            preview_path,
                            fps=preview_fps,
                            size=self.telegram_preview_size,
                            crf=28,
                            min_size=1_000,
                        ):
                            self.notifier.send_telegram(
                                self._telegram_message(f"{msg} - local MP4 saved"),
                                preview_path,
                                bypass_cooldown=bypass_telegram_cooldown,
                            )
                            self.db.log_event("media", "mp4_preview", 0.0, preview_path)
                        else:
                            self.notifier.send_telegram(
                                self._telegram_message(f"{msg} - local MP4 saved"),
                                bypass_cooldown=bypass_telegram_cooldown,
                            )
                    elif media_mode == "snapshot":
                        jpg_path = os.path.join(label_folder, self._media_name(label, timestamp, ".jpg"))
                        self.notifier.send_telegram(
                            self._telegram_message(f"{msg} - local MP4 saved"),
                            jpg_path,
                            bypass_cooldown=bypass_telegram_cooldown,
                        )
                    elif media_mode == "text":
                        self.notifier.send_telegram(
                            self._telegram_message(f"{msg} - local MP4 saved"),
                            bypass_cooldown=bypass_telegram_cooldown,
                        )
                    elif media_mode == "none":
                        logging.info(f"Telegram media skipped; local MP4 saved: {mp4_path}")
                    else:
                        logging.warning(f"Unknown telegram.media_mode={media_mode}; sending text only")
                        self.notifier.send_telegram(
                            self._telegram_message(f"{msg} - local MP4 saved"),
                            bypass_cooldown=bypass_telegram_cooldown,
                        )

                # Fallback to GIF
                gif_created = False
                if not mp4_created:
                    gif_path = os.path.join(label_folder, self._media_name(label, timestamp, ".gif"))
                    if self.notifier.create_gif(frames, gif_path):
                        msg = f"{label.title()} detected"
                        if person_name:
                            msg += f" ({person_name})"
                        if send_telegram:
                            self.notifier.send_telegram(
                                self._telegram_message(msg), gif_path, bypass_cooldown=bypass_telegram_cooldown
                            )
                        else:
                            logging.info(
                                "Telegram GIF skipped by event score; local GIF saved "
                                f"(score={event_score}, path={gif_path})"
                            )
                        self.db.log_event("media", "gif", 0.0, gif_path)
                        gif_created = True

                # Fallback to JPEG snapshot
                if not mp4_created and not gif_created:
                    jpg_path = os.path.join(label_folder, self._media_name(label, timestamp, ".jpg"))
                    if os.path.exists(jpg_path) and send_telegram:
                        msg = f"{label.title()} detected"
                        if person_name:
                            msg += f" ({person_name})"
                        self.notifier.send_telegram(
                            self._telegram_message(msg), jpg_path, bypass_cooldown=bypass_telegram_cooldown
                        )
                        self.db.log_event("media", "jpg_notify", 0.0, jpg_path)

                self.notification_queue.task_done()
                self._run_scheduled_cleanup(time.time())
            except queue.Empty:
                self._run_scheduled_cleanup(time.time())
                continue
            except Exception as e:
                logging.error(f"Notification worker error: {e}")

    def stop(self) -> None:
        self.running = False
