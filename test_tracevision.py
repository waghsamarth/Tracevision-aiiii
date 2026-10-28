import os, sqlite3, tempfile, time, unittest
import numpy as np
from app import create_app
from camera import MotionDetector, WARMUP_FRAMES
from db import ApiError, Database


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "test.db")
        self.app = create_app(self.path)
        self.c, self.db = self.app.test_client(), self.app.db

    def tearDown(self):
        self.app.cam.shutdown()
        time.sleep(0.2)
        self.dir.cleanup()

    def post(self, url, body=None):
        return self.c.post(url, json=body if body is not None else {})

    def ack_event(self):
        e = self.db.add_event("simulated", "tampering", "high", "demo")
        self.db.set_event_status(e["id"], "acknowledged")
        return e["id"]


class TestDatabase(Base):
    def test_init_creates_tables_and_survives_reopen(self):
        names = {r[0] for r in sqlite3.connect(self.path).execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"events", "incidents", "incident_notes", "audit", "settings"} <= names)
        self.db.add_event("motion", "motion_candidate", "low")
        self.assertEqual(Database(self.path).metrics()["events_total"], 1)  # persistence

    def test_event_storage_and_constraints(self):
        e = self.db.add_event("motion", "motion_candidate", "low", "note")
        self.assertEqual((e["status"], e["source_type"]), ("new", "motion"))
        with self.assertRaises(ApiError): self.db.add_event("bogus", "motion_candidate", "low")
        with self.assertRaises(ApiError): self.db.add_event("motion", "motion_candidate", "extreme")
        with self.assertRaises(sqlite3.IntegrityError):
            sqlite3.connect(self.path).execute("INSERT INTO events(ts,source_type,event_type,severity,updated_ts) VALUES('x','alien','t','low','x')")

    def test_sql_injection_text_is_stored_literally(self):
        e = self.db.add_event("simulated", "tampering", "low", "x'); DROP TABLE events;--")
        self.assertEqual(len(self.db.list_events(q="DROP")), 1)
        self.assertEqual(self.db.get_event(e["id"])["id"], e["id"])


class TestAlertTransitions(Base):
    def test_valid_path_to_closed(self):
        e = self.db.add_event("motion", "motion_candidate", "low")
        for s in ("acknowledged", "reviewed", "verified", "closed"):
            self.assertEqual(self.db.set_event_status(e["id"], s)["status"], s)

    def test_invalid_transitions_rejected_with_409(self):
        e = self.db.add_event("motion", "motion_candidate", "low")
        r = self.post(f"/api/events/{e['id']}/status", {"status": "verified"})
        self.assertEqual(r.status_code, 409)  # candidate cannot skip human review
        self.assertEqual(self.post(f"/api/events/{e['id']}/status", {"status": "nonsense"}).status_code, 400)
        self.assertEqual(self.post("/api/events/999/status", {"status": "acknowledged"}).status_code, 404)

    def test_dismissed_cannot_be_verified(self):
        e = self.db.add_event("motion", "motion_candidate", "low")
        self.db.set_event_status(e["id"], "dismissed")
        with self.assertRaises(ApiError): self.db.set_event_status(e["id"], "verified")


class TestIncidents(Base):
    def test_requires_acknowledged_event(self):
        e = self.db.add_event("motion", "motion_candidate", "low")
        self.assertEqual(self.post(f"/api/events/{e['id']}/incident").status_code, 409)
        self.db.set_event_status(e["id"], "acknowledged")
        r = self.post(f"/api/events/{e['id']}/incident")
        self.assertEqual(r.status_code, 201)
        self.assertFalse(r.json["human_verified"])  # candidate is not a verified incident
        self.assertEqual(self.post(f"/api/events/{e['id']}/incident").status_code, 409)

    def test_lifecycle_notes_search_persistence(self):
        i = self.db.create_incident("Door camera check", "details", "high")
        with self.assertRaises(ApiError): self.db.set_incident_status(i["id"], "resolved")  # needs summary
        self.db.set_incident_status(i["id"], "investigating")
        self.db.add_note(i["id"], "Checked footage")
        self.db.set_incident_status(i["id"], "resolved", "False alarm")
        closed = self.db.set_incident_status(i["id"], "closed")
        self.assertEqual((closed["status"], closed["resolution"]), ("closed", "False alarm"))
        with self.assertRaises(ApiError): self.db.add_note(i["id"], "late")
        again = Database(self.path)
        self.assertEqual(len(again.list_incidents(q="door")), 1)
        self.assertEqual(len(again.get_incident(i["id"])["notes"]), 1)

    def test_validation(self):
        self.assertEqual(self.post("/api/incidents", {"title": "  "}).status_code, 400)
        self.assertEqual(self.post("/api/incidents", {"title": "x" * 500}).status_code, 400)
        self.assertEqual(self.post("/api/incidents", {"title": "ok", "severity": "wild"}).status_code, 400)


class TestAudit(Base):
    def actions(self):
        return [a["action"] for a in self.db.list_audit()]

    def test_actions_are_logged(self):
        eid = self.ack_event()
        self.db.create_incident("t", event_id=eid)
        self.db.update_settings({"sensitivity": 7})
        for a in ("app_started", "event_created", "alert_acknowledged", "incident_created", "settings_changed"):
            self.assertIn(a, self.actions())

    def test_verify_detects_tampering(self):
        self.ack_event()
        self.assertTrue(self.db.verify_audit()["valid"])
        con = sqlite3.connect(self.path)
        con.execute("UPDATE audit SET detail='edited' WHERE id=2"); con.commit()
        r = self.c.get("/api/audit/verify").json
        self.assertFalse(r["valid"]); self.assertEqual(r["first_bad_id"], 2)

    def test_failed_transaction_leaves_no_partial_audit(self):
        before = len(self.db.list_audit())
        with self.assertRaises(ApiError): self.db.set_event_status(12345, "acknowledged")
        self.assertEqual(len(self.db.list_audit()), before)


class TestApi(Base):
    def test_health(self):
        r = self.c.get("/api/health")
        self.assertEqual((r.status_code, r.json["status"], r.json["database"]), (200, "ok", "ok"))

    def test_rejects_non_json_and_bad_input(self):
        self.assertEqual(self.c.post("/api/demo/event", data="x=1").status_code, 400)
        self.assertEqual(self.post("/api/demo/event", {"event_type": "motion_candidate"}).status_code, 400)  # demos cannot fake motion
        self.assertEqual(self.post("/api/camera/scan", {"enabled": "yes"}).status_code, 400)
        self.assertEqual(self.post("/api/settings", {"sensitivity": 99}).status_code, 400)
        self.assertEqual(self.post("/api/settings", {"nope": 1}).status_code, 400)
        self.assertEqual(self.c.get("/api/events?limit=abc").status_code, 400)
        self.assertEqual(self.c.get("/api/nothing").status_code, 404)
        self.assertEqual(self.c.get("/api/events/1").status_code, 404)

    def test_scan_and_stop_need_running_camera(self):
        self.assertEqual(self.post("/api/camera/scan", {"enabled": True}).status_code, 409)
        self.assertEqual(self.post("/api/camera/stop").status_code, 409)

    def test_demo_events_are_labelled_simulated(self):
        r = self.post("/api/demo/event", {"event_type": "tampering"})
        self.assertEqual((r.status_code, r.json["source_type"]), (201, "simulated"))
        self.assertIn("DEMO", r.json["note"])

    def test_dashboard_metrics_come_from_database(self):
        self.assertEqual(self.c.get("/api/dashboard").json["metrics"]["events_total"], 0)
        self.post("/api/demo/event", {"event_type": "tampering"})
        self.db.add_event("motion", "motion_candidate", "low")
        m = self.c.get("/api/dashboard").json["metrics"]
        self.assertEqual((m["events_total"], m["events_simulated"], m["events_real_motion"], m["events_new"]), (2, 1, 1, 2))

    def test_csv_export_neutralises_formulas(self):
        self.db.add_event("simulated", "tampering", "low", "=HYPERLINK(\"http://x\")")
        r = self.c.get("/api/export/events.csv")
        self.assertIn("'=HYPERLINK", r.get_data(as_text=True))

    def test_index_page_served(self):
        self.assertEqual(self.c.get("/").status_code, 200)


class TestMotionAndCamera(Base):
    def frame(self, box=None):
        f = np.full((240, 320, 3), 90, np.uint8)
        if box: x, y, w, h = box; f[y:y+h, x:x+w] = 250
        return f

    def test_static_scene_gives_no_motion(self):
        d = MotionDetector(5)
        for _ in range(WARMUP_FRAMES + 20): self.assertEqual(d.process(self.frame()), [])

    def test_moving_block_is_detected(self):
        d = MotionDetector(5)
        for _ in range(WARMUP_FRAMES + 3): d.process(self.frame())
        self.assertTrue(d.process(self.frame((100, 60, 80, 90))))

    def test_sensitivity_changes_minimum_area(self):
        low, high = MotionDetector(1), MotionDetector(10)
        self.assertGreater(low.min_area_frac, high.min_area_frac)

    def test_global_lighting_change_is_ignored(self):
        d = MotionDetector(10)
        for _ in range(WARMUP_FRAMES + 3): d.process(self.frame())
        self.assertEqual(d.process(np.full((240, 320, 3), 200, np.uint8)), [])

    def test_missing_camera_reports_unavailable_and_logs_event(self):
        self.db.update_settings({"camera_index": 9})
        self.post("/api/camera/start")
        for _ in range(50):
            if self.c.get("/api/camera/status").json["state"] != "starting": break
            time.sleep(0.1)
        st = self.c.get("/api/camera/status").json
        if st["state"] == "running":  # a real camera exists at index 9
            self.post("/api/camera/stop"); self.skipTest("camera present at index 9")
        self.assertIn(st["state"], ("unavailable", "error"))
        self.assertEqual(st["mode"], "simulation")
        self.assertEqual(self.db.metrics()["events_camera_errors"], 1)


class FakeCap:
    """Stands in for cv2.VideoCapture: still scene, then a bright block appears."""
    released = False
    def __init__(self, *a): self.n = 0
    def isOpened(self): return True
    def set(self, *a): pass
    def read(self):
        time.sleep(0.01); self.n += 1
        f = np.full((240, 320, 3), 90, np.uint8)
        if self.n > 40: f[60:160, 100:220] = 250
        return True, f
    def release(self): FakeCap.released = True


class TestCaptureLoop(Base):
    def test_fake_camera_produces_single_motion_candidate_and_releases(self):
        import camera
        real = camera.cv2.VideoCapture
        camera.cv2.VideoCapture = FakeCap
        try:
            self.post("/api/camera/start")
            for _ in range(30):
                if self.c.get("/api/camera/status").json["state"] == "running": break
                time.sleep(0.1)
            self.assertEqual(self.post("/api/camera/start").status_code, 409)  # no duplicate handle
            self.assertEqual(self.post("/api/camera/scan", {"enabled": True}).status_code, 200)
            time.sleep(2.5)
            st = self.c.get("/api/camera/status").json
            self.assertTrue(st["scanning"]); self.assertGreater(st["frames"], 20)
            ev = self.db.list_events(source="motion")
            self.assertEqual(len(ev), 1)  # cooldown prevents duplicates
            self.assertEqual(ev[0]["source_type"], "motion")
            self.assertEqual(self.post("/api/camera/stop").status_code, 200)
            self.assertTrue(FakeCap.released)
            self.assertEqual(self.c.get("/api/camera/status").json["state"], "stopped")
        finally:
            camera.cv2.VideoCapture = real


if __name__ == "__main__":
    unittest.main()
