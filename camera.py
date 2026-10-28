"""Webcam capture thread and local motion detection (frame differencing).

Motion is only a candidate signal. Nothing here identifies people or objects."""
import atexit, os, threading, time
import cv2
import numpy as np
from db import ApiError

WARMUP_FRAMES = 15


class MotionDetector:
    def __init__(self, sensitivity=5):
        self.set_sensitivity(sensitivity)
        self.reset()

    def set_sensitivity(self, s):
        self.threshold = int(60 - 4 * s)              # pixel difference needed (56 .. 20)
        self.min_area_frac = 0.033 - 0.003 * s         # share of frame that must change

    def reset(self):
        self.bg, self.frames = None, 0

    def process(self, frame):
        """Return a list of (x, y, w, h) regions with motion."""
        gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (21, 21), 0)
        self.frames += 1
        if self.bg is None:
            self.bg = gray.astype("float32")
            return []
        delta = cv2.absdiff(gray, cv2.convertScaleAbs(self.bg))
        cv2.accumulateWeighted(gray, self.bg, 0.05)
        if self.frames <= WARMUP_FRAMES:
            return []
        mask = cv2.dilate(cv2.threshold(delta, self.threshold, 255, cv2.THRESH_BINARY)[1], None, iterations=2)
        if cv2.countNonZero(mask) > 0.6 * mask.size:   # whole-frame change = lighting/exposure shift
            self.bg = gray.astype("float32")
            return []
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        min_area = self.min_area_frac * mask.size
        return [cv2.boundingRect(c) for c in cnts if cv2.contourArea(c) >= min_area]


def _placeholder(text, sub):
    img = np.zeros((360, 640, 3), np.uint8)
    img[:] = (11, 7, 3)
    cv2.putText(img, text, (40, 170), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 229, 0), 2, cv2.LINE_AA)
    cv2.putText(img, sub, (40, 210), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (180, 180, 170), 1, cv2.LINE_AA)
    return cv2.imencode(".jpg", img)[1].tobytes()


PLACEHOLDER = _placeholder("SIMULATION MODE", "No webcam feed. Events here are demo only.")


class CameraManager:
    def __init__(self, db):
        self.db, self.lock = db, threading.Lock()
        self.thread, self.stop_evt = None, threading.Event()
        self.state, self.message = "stopped", "Camera is off."
        self.scanning, self.jpeg = False, PLACEHOLDER
        self.last_frame_ts = self.last_event_ts = 0.0
        self.last_motion_event_id, self.fps, self.frames, self.size = None, 0.0, 0, None
        self.detector = MotionDetector(db.get_settings()["sensitivity"])
        atexit.register(self.shutdown)

    def status(self):
        with self.lock:
            alive = self.thread is not None and self.thread.is_alive()
            return {"state": self.state, "message": self.message, "scanning": self.scanning,
                    "fps": round(self.fps, 1), "frames": self.frames, "resolution": self.size,
                    "stalled": self.state == "running" and time.time() - self.last_frame_ts > 5,
                    "last_motion_event_id": self.last_motion_event_id, "thread_alive": alive,
                    "mode": "live" if self.state == "running" else "simulation"}

    def start(self):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise ApiError(409, "The camera is already running.")
            self.stop_evt = threading.Event()
            self.state, self.message, self.scanning = "starting", "Opening webcam...", False
            self.thread = threading.Thread(target=self._run, args=(self.stop_evt,), daemon=True)
            self.thread.start()
        self.db.audit("camera_start_requested", "", "operator")

    def stop(self):
        with self.lock:
            t, evt = self.thread, self.stop_evt
            if t is None or not t.is_alive():
                raise ApiError(409, "The camera is not running.")
        evt.set()
        t.join(timeout=5)
        self.db.audit("camera_stopped", "", "operator")

    def set_scanning(self, enabled):
        with self.lock:
            if enabled and self.state != "running":
                raise ApiError(409, "Start the camera before turning scanning on.")
            self.scanning = enabled
            self.detector.reset()
        self.db.audit("scan_on" if enabled else "scan_off", "", "operator")

    def apply_settings(self, s):
        self.detector.set_sensitivity(s["sensitivity"])

    def shutdown(self):
        if self.thread and self.thread.is_alive():
            self.stop_evt.set()
            self.thread.join(timeout=3)

    def _fail(self, state, msg):
        try:
            self.db.add_event("camera", "camera_error", "medium", msg)  # record first, then publish state
        finally:
            with self.lock:
                self.state, self.message, self.scanning, self.jpeg = state, msg, False, PLACEHOLDER

    def _run(self, stop):
        cap = None
        try:
            idx = self.db.get_settings()["camera_index"]
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(idx)
            if not cap.isOpened():
                self._fail("unavailable", f"Could not open camera {idx}. It may be missing, in use, or blocked by privacy settings.")
                return
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640); cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            with self.lock:
                self.state, self.message, self.frames = "running", "Webcam is live.", 0
                self.last_frame_ts = time.time()
            self.db.audit("camera_started", f"index {idx}")
            failures, t0, n = 0, time.time(), 0
            while not stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    failures += 1
                    if failures >= 30:
                        self._fail("error", "The webcam stopped sending frames.")
                        return
                    time.sleep(0.05); continue
                failures, n = 0, n + 1
                rects = []
                if self.scanning:
                    rects = self.detector.process(frame)
                    for x, y, w, h in rects:
                        cv2.rectangle(frame, (x, y), (x + w, y + h), (157, 255, 0), 2)
                    self._maybe_event(rects, frame.shape)
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                with self.lock:
                    if ok: self.jpeg = buf.tobytes()
                    self.frames, self.last_frame_ts, self.size = self.frames + 1, time.time(), [frame.shape[1], frame.shape[0]]
                    if time.time() - t0 >= 2: self.fps, t0, n = n / (time.time() - t0), time.time(), 0
        except Exception as exc:  # keep the server alive whatever the driver does
            self._fail("error", f"Camera error: {exc}")
        finally:
            if cap is not None: cap.release()
            with self.lock:
                if self.state in ("running", "starting"):
                    self.state, self.message = "stopped", "Camera is off."
                self.scanning, self.jpeg, self.fps = False, PLACEHOLDER, 0.0

    def _maybe_event(self, rects, shape):
        cooldown = self.db.get_settings()["cooldown"]
        if not rects or time.time() - self.last_event_ts < cooldown:
            return
        share = max(w * h for _, _, w, h in rects) / (shape[0] * shape[1])
        ev = self.db.add_event("motion", "motion_candidate", "medium" if share > 0.25 else "low",
                               f"Motion in {len(rects)} region(s); largest covers {share:.0%} of the frame. Not verified.")
        with self.lock:
            self.last_event_ts, self.last_motion_event_id = time.time(), ev["id"]
