"""TraceVision AI 2.0 - local Flask server. Binds to 127.0.0.1 only. No authentication."""
import csv, io, os, sys, time
from flask import Flask, Response, jsonify, render_template, request
from camera import CameraManager
from db import ApiError, Database, EVENT_TYPES

BASE = os.path.dirname(os.path.abspath(__file__))
DEMO_TYPES = ("camera_disconnected", "tampering", "restricted_zone")
STARTED = time.time()


def create_app(db_path=None, start_camera_manager=True):
    app = Flask(__name__)
    db = Database(db_path or os.path.join(BASE, "data", "tracevision.db"))
    cam = CameraManager(db)
    app.db, app.cam = db, cam
    db.audit("app_started", "")

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ApiError(400, "Send a JSON object with Content-Type: application/json.")
        return data

    @app.errorhandler(ApiError)
    def api_error(e): return jsonify(error=e.message), e.status
    @app.errorhandler(404)
    def nf(e): return jsonify(error="Not found."), 404
    @app.errorhandler(405)
    def na(e): return jsonify(error="Method not allowed."), 405
    @app.errorhandler(Exception)
    def boom(e):
        app.logger.exception(e)
        return jsonify(error="Internal error. See the server console."), 500

    @app.after_request
    def headers(r):
        r.headers["X-Content-Type-Options"] = "nosniff"
        r.headers["Cache-Control"] = "no-store"
        return r

    @app.get("/")
    def index(): return render_template("index.html")

    @app.get("/api/health")
    def health():
        db.metrics()
        return jsonify(status="ok", database="ok", camera=cam.status()["state"], uptime_s=int(time.time() - STARTED),
                       host=request.host, note="Local only. No authentication.")

    @app.get("/api/dashboard")
    def dashboard(): return jsonify(metrics=db.metrics(), camera=cam.status())

    @app.get("/api/camera/status")
    def cam_status(): return jsonify(cam.status())
    @app.post("/api/camera/start")
    def cam_start(): cam.start(); return jsonify(cam.status()), 202
    @app.post("/api/camera/stop")
    def cam_stop(): cam.stop(); return jsonify(cam.status())
    @app.post("/api/camera/scan")
    def cam_scan():
        v = body().get("enabled")
        if not isinstance(v, bool): raise ApiError(400, "'enabled' must be true or false.")
        cam.set_scanning(v); return jsonify(cam.status())

    @app.get("/video_feed")
    def video_feed():
        def gen():
            last = None
            while True:
                jpg = cam.jpeg
                if jpg is not last:
                    last = jpg
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
                time.sleep(0.04)
        return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.get("/api/events")
    def events():
        a = request.args
        lim = a.get("limit", "200")
        if not lim.isdigit() or not 1 <= int(lim) <= 1000: raise ApiError(400, "limit must be 1-1000.")
        return jsonify(db.list_events(a.get("source", ""), a.get("status", ""), a.get("severity", ""),
                                      a.get("q", "").strip()[:100], a.get("active") == "1", int(lim)))

    @app.get("/api/events/<int:eid>")
    def event(eid): return jsonify(db.get_event(eid))

    @app.post("/api/events/<int:eid>/status")
    def event_status(eid):
        d = body(); return jsonify(db.set_event_status(eid, d.get("status"), d.get("note", "")))

    @app.post("/api/events/<int:eid>/incident")
    def event_incident(eid):
        ev, d = db.get_event(eid), body()
        title = d.get("title") or f"{ev['event_type'].replace('_', ' ').capitalize()} (event {eid})"
        return jsonify(db.create_incident(title, d.get("description", ""), ev["severity"], eid)), 201

    @app.post("/api/demo/event")
    def demo_event():
        t = body().get("event_type")
        if t not in DEMO_TYPES: raise ApiError(400, f"event_type must be one of {', '.join(DEMO_TYPES)}.")
        return jsonify(db.add_event("simulated", t, "high" if t == "tampering" else "medium", "DEMO event. Not a real detection.")), 201

    @app.get("/api/incidents")
    def incidents(): return jsonify(db.list_incidents(request.args.get("q", "").strip()[:100], request.args.get("status", "")))
    @app.post("/api/incidents")
    def incident_new():
        d = body()
        return jsonify(db.create_incident(d.get("title"), d.get("description", ""), d.get("severity", "medium"))), 201
    @app.get("/api/incidents/<int:iid>")
    def incident(iid): return jsonify(db.get_incident(iid))
    @app.post("/api/incidents/<int:iid>/status")
    def incident_status(iid):
        d = body(); return jsonify(db.set_incident_status(iid, d.get("status"), d.get("resolution", "")))
    @app.post("/api/incidents/<int:iid>/notes")
    def incident_note(iid): return jsonify(db.add_note(iid, body().get("text"))), 201

    @app.get("/api/audit")
    def audit(): return jsonify(db.list_audit())
    @app.get("/api/audit/verify")
    def audit_verify():
        r = db.verify_audit()
        r["note"] = "Detects edits inside the log. It does not stop someone with file access from deleting the newest entries or rewriting everything."
        return jsonify(r)

    @app.get("/api/settings")
    def settings_get(): return jsonify(db.get_settings())
    @app.post("/api/settings")
    def settings_set():
        s = db.update_settings(body()); cam.apply_settings(s); return jsonify(s)

    @app.get("/api/export/events.csv")
    def export():
        out = io.StringIO(); w = csv.writer(out)
        w.writerow(["id", "timestamp_utc", "source", "type", "severity", "status", "note"])
        guard = lambda v: "'" + v if isinstance(v, str) and v[:1] in "=+-@\t\r" else v
        for e in db.list_events(limit=1000):
            w.writerow([guard(e[k]) for k in ("id", "ts", "source_type", "event_type", "severity", "status", "note")])
        db.audit("events_exported", "csv", "operator")
        return Response(out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=tracevision_events.csv"})

    return app


if __name__ == "__main__":
    application = create_app()
    print("TraceVision AI running at http://127.0.0.1:5000  (Ctrl+C to stop)")
    application.run(host="127.0.0.1", port=5000, threaded=True, debug=False)
