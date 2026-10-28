"""SQLite storage: events, incidents, settings and a hash-chained audit log."""
import hashlib, sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


SOURCES = ("camera", "motion", "simulated")
EVENT_TYPES = ("camera_error", "motion_candidate", "camera_disconnected", "tampering", "restricted_zone")
SEVERITIES = ("low", "medium", "high")
EVENT_TRANSITIONS = {
    "new": ("acknowledged", "dismissed"),
    "acknowledged": ("reviewed", "dismissed"),
    "reviewed": ("verified", "dismissed"),
    "verified": ("closed",),
    "dismissed": ("closed",),
    "closed": (),
}
INCIDENT_TRANSITIONS = {
    "open": ("investigating", "resolved"),
    "investigating": ("resolved",),
    "resolved": ("closed", "investigating"),
    "closed": (),
}
DEFAULT_SETTINGS = {"sensitivity": 5, "cooldown": 10, "camera_index": 0}
SETTING_RANGES = {"sensitivity": (1, 10), "cooldown": (1, 300), "camera_index": (0, 9)}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN ('camera','motion','simulated')),
  event_type TEXT NOT NULL, severity TEXT NOT NULL CHECK(severity IN ('low','medium','high')),
  status TEXT NOT NULL DEFAULT 'new', note TEXT NOT NULL DEFAULT '', updated_ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incidents(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, updated_ts TEXT NOT NULL,
  title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  severity TEXT NOT NULL CHECK(severity IN ('low','medium','high')),
  status TEXT NOT NULL DEFAULT 'open', resolution TEXT NOT NULL DEFAULT '',
  event_id INTEGER UNIQUE REFERENCES events(id));
CREATE TABLE IF NOT EXISTS incident_notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER NOT NULL REFERENCES incidents(id),
  ts TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, detail TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(prev, ts, actor, action, detail):
    return hashlib.sha256("|".join((prev, ts, actor, action, detail)).encode()).hexdigest()


def clean_text(value, field, max_len, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError(400, f"{field} must be text.")
    value = value.strip()
    if required and not value:
        raise ApiError(400, f"{field} is required.")
    if len(value) > max_len:
        raise ApiError(400, f"{field} must be at most {max_len} characters.")
    return value


class Database:
    def __init__(self, path):
        self.path = str(path)
        c = sqlite3.connect(self.path)
        c.executescript(SCHEMA)
        c.commit()
        c.close()

    @contextmanager
    def tx(self):
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()

    def _audit(self, c, action, detail, actor="system"):
        row = c.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        prev, ts = (row["hash"] if row else "GENESIS"), now()
        c.execute("INSERT INTO audit(ts,actor,action,detail,prev_hash,hash) VALUES(?,?,?,?,?,?)",
                  (ts, actor, action, detail, prev, _hash(prev, ts, actor, action, detail)))

    def audit(self, action, detail="", actor="system"):
        with self.tx() as c:
            self._audit(c, action, detail, actor)

    def list_audit(self, limit=200):
        with self.tx() as c:
            return [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]

    def verify_audit(self):
        """Recompute the hash chain. Detects edits or deletions in the middle of the
        log. It cannot detect removal of the newest entries or a full rewrite."""
        with self.tx() as c:
            rows = c.execute("SELECT * FROM audit ORDER BY id").fetchall()
        prev = "GENESIS"
        for r in rows:
            if r["prev_hash"] != prev or r["hash"] != _hash(prev, r["ts"], r["actor"], r["action"], r["detail"]):
                return {"valid": False, "entries": len(rows), "first_bad_id": r["id"]}
            prev = r["hash"]
        return {"valid": True, "entries": len(rows), "first_bad_id": None}

    # ---- events
    @staticmethod
    def _event(r):
        d = dict(r)
        d["allowed"] = list(EVENT_TRANSITIONS.get(d["status"], ()))
        return d

    def add_event(self, source_type, event_type, severity, note=""):
        if source_type not in SOURCES: raise ApiError(400, "Invalid source type.")
        if event_type not in EVENT_TYPES: raise ApiError(400, "Invalid event type.")
        if severity not in SEVERITIES: raise ApiError(400, "Invalid severity.")
        note, ts = clean_text(note, "note", 500), now()
        with self.tx() as c:
            cur = c.execute("INSERT INTO events(ts,source_type,event_type,severity,note,updated_ts) VALUES(?,?,?,?,?,?)",
                            (ts, source_type, event_type, severity, note, ts))
            self._audit(c, "event_created", f"event {cur.lastrowid}: {source_type}/{event_type}/{severity}")
            return self._event(c.execute("SELECT * FROM events WHERE id=?", (cur.lastrowid,)).fetchone())

    def get_event(self, eid):
        with self.tx() as c:
            r = c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if not r: raise ApiError(404, "Event not found.")
        return self._event(r)

    def list_events(self, source="", status="", severity="", q="", active=False, limit=200):
        sql, args = "SELECT * FROM events WHERE 1=1", []
        for col, val in (("source_type", source), ("status", status), ("severity", severity)):
            if val: sql += f" AND {col}=?"; args.append(val)
        if q:
            sql += " AND (note LIKE ? OR event_type LIKE ? OR CAST(id AS TEXT)=?)"
            args += [f"%{q}%", f"%{q}%", q]
        if active: sql += " AND status NOT IN ('dismissed','closed')"
        sql += " ORDER BY id DESC LIMIT ?"
        with self.tx() as c:
            return [self._event(r) for r in c.execute(sql, args + [limit])]

    def set_event_status(self, eid, new, note=""):
        note = clean_text(note, "note", 500)
        with self.tx() as c:
            r = c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
            if not r: raise ApiError(404, "Event not found.")
            if new not in EVENT_TRANSITIONS: raise ApiError(400, "Unknown status.")
            if new not in EVENT_TRANSITIONS[r["status"]]:
                raise ApiError(409, f"Cannot change an event from '{r['status']}' to '{new}'.")
            text = (r["note"] + ("\n" if r["note"] and note else "") + note) if note else r["note"]
            c.execute("UPDATE events SET status=?, note=?, updated_ts=? WHERE id=?", (new, text[:1000], now(), eid))
            self._audit(c, "alert_" + new, f"event {eid}: {r['status']} -> {new}", "operator")
            return self._event(c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone())

    # ---- incidents
    def _incident(self, c, r, notes=False):
        d = dict(r)
        if d["event_id"]:
            e = c.execute("SELECT source_type, status FROM events WHERE id=?", (d["event_id"],)).fetchone()
            d["event_source"], d["event_status"] = e["source_type"], e["status"]
            d["human_verified"] = e["status"] in ("verified", "closed") and e["source_type"] != "camera"
        else:
            d["event_source"], d["event_status"], d["human_verified"] = None, None, False
        d["allowed"] = list(INCIDENT_TRANSITIONS[d["status"]])
        if notes:
            d["notes"] = [dict(n) for n in c.execute("SELECT * FROM incident_notes WHERE incident_id=? ORDER BY id", (d["id"],))]
        return d

    def create_incident(self, title, description="", severity="medium", event_id=None):
        title = clean_text(title, "title", 120, required=True)
        description = clean_text(description, "description", 1000)
        if severity not in SEVERITIES: raise ApiError(400, "Invalid severity.")
        ts = now()
        with self.tx() as c:
            if event_id is not None:
                e = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
                if not e: raise ApiError(404, "Event not found.")
                if e["status"] not in ("acknowledged", "reviewed", "verified"):
                    raise ApiError(409, "Acknowledge the alert before creating an incident from it.")
                if c.execute("SELECT 1 FROM incidents WHERE event_id=?", (event_id,)).fetchone():
                    raise ApiError(409, "This event already has an incident.")
            cur = c.execute("INSERT INTO incidents(ts,updated_ts,title,description,severity,event_id) VALUES(?,?,?,?,?,?)",
                            (ts, ts, title, description, severity, event_id))
            self._audit(c, "incident_created", f"incident {cur.lastrowid}" + (f" from event {event_id}" if event_id else ""), "operator")
            return self._incident(c, c.execute("SELECT * FROM incidents WHERE id=?", (cur.lastrowid,)).fetchone(), True)

    def list_incidents(self, q="", status=""):
        sql, args = "SELECT * FROM incidents WHERE 1=1", []
        if status: sql += " AND status=?"; args.append(status)
        if q:
            sql += " AND (title LIKE ? OR description LIKE ? OR resolution LIKE ? OR CAST(id AS TEXT)=?)"
            args += [f"%{q}%"] * 3 + [q]
        with self.tx() as c:
            return [self._incident(c, r) for r in c.execute(sql + " ORDER BY id DESC LIMIT 200", args)]

    def get_incident(self, iid):
        with self.tx() as c:
            r = c.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
            if not r: raise ApiError(404, "Incident not found.")
            return self._incident(c, r, True)

    def set_incident_status(self, iid, new, resolution=""):
        resolution = clean_text(resolution, "resolution", 1000)
        with self.tx() as c:
            r = c.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
            if not r: raise ApiError(404, "Incident not found.")
            if new not in INCIDENT_TRANSITIONS: raise ApiError(400, "Unknown status.")
            if new not in INCIDENT_TRANSITIONS[r["status"]]:
                raise ApiError(409, f"Cannot change an incident from '{r['status']}' to '{new}'.")
            if new == "resolved" and not resolution:
                raise ApiError(400, "A resolution summary is required to resolve an incident.")
            c.execute("UPDATE incidents SET status=?, resolution=?, updated_ts=? WHERE id=?",
                      (new, resolution or r["resolution"], now(), iid))
            self._audit(c, "incident_" + new, f"incident {iid}: {r['status']} -> {new}", "operator")
            return self._incident(c, c.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone(), True)

    def add_note(self, iid, text):
        text = clean_text(text, "note", 1000, required=True)
        with self.tx() as c:
            r = c.execute("SELECT status FROM incidents WHERE id=?", (iid,)).fetchone()
            if not r: raise ApiError(404, "Incident not found.")
            if r["status"] == "closed": raise ApiError(409, "Closed incidents cannot be edited.")
            c.execute("INSERT INTO incident_notes(incident_id,ts,text) VALUES(?,?,?)", (iid, now(), text))
            self._audit(c, "incident_note", f"incident {iid}", "operator")
            return self._incident(c, c.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone(), True)

    # ---- settings and metrics
    def get_settings(self):
        with self.tx() as c:
            stored = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}
        return {k: int(stored.get(k, v)) for k, v in DEFAULT_SETTINGS.items()}

    def update_settings(self, data):
        changes = {}
        for k, v in data.items():
            if k not in SETTING_RANGES: raise ApiError(400, f"Unknown setting '{k}'.")
            lo, hi = SETTING_RANGES[k]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ApiError(400, f"{k} must be a whole number from {lo} to {hi}.")
            changes[k] = v
        with self.tx() as c:
            for k, v in changes.items():
                c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
            if changes: self._audit(c, "settings_changed", ", ".join(f"{k}={v}" for k, v in changes.items()), "operator")
        return self.get_settings()

    def metrics(self):
        with self.tx() as c:
            one = lambda sql, *a: c.execute(sql, a).fetchone()[0]
            return {
                "events_total": one("SELECT COUNT(*) FROM events"),
                "events_new": one("SELECT COUNT(*) FROM events WHERE status='new'"),
                "events_needing_review": one("SELECT COUNT(*) FROM events WHERE status IN ('new','acknowledged')"),
                "events_real_motion": one("SELECT COUNT(*) FROM events WHERE source_type='motion'"),
                "events_simulated": one("SELECT COUNT(*) FROM events WHERE source_type='simulated'"),
                "events_camera_errors": one("SELECT COUNT(*) FROM events WHERE source_type='camera'"),
                "events_verified": one("SELECT COUNT(*) FROM events WHERE status='verified'"),
                "incidents_open": one("SELECT COUNT(*) FROM incidents WHERE status IN ('open','investigating')"),
                "incidents_total": one("SELECT COUNT(*) FROM incidents"),
                "audit_entries": one("SELECT COUNT(*) FROM audit"),
            }
