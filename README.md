# TraceVision AI 2.0

A local camera-monitoring and incident-review tool. It opens your laptop webcam, looks for **motion**, and helps a person review each alert and track incidents. It runs only on your own computer (127.0.0.1).

## What it does and does not do

- **Real motion candidates**: frame differencing on the webcam. Motion means pixels changed. It is not proof of a person, a threat, or a crime. A person must review every alert.
- **Camera errors**: recorded when the webcam cannot be opened or stops sending frames.
- **Demo events**: buttons on the Live Camera page add events labelled DEMO / simulated. They are never mixed in as real detections.
- No face recognition, no identity matching, no object detection.
- Video stays on this computer. Frames are streamed to your browser only and are never saved or uploaded.
- There is **no login**. Anyone who can open http://127.0.0.1:5000 on this machine can use it. Do not expose it to a network.

## Run on Windows (easiest)

1. Install Python 3.10 or newer from python.org. Tick **Add python.exe to PATH** in the installer.
2. Double-click `run.bat`. The first run creates `.venv` and installs packages, then opens the browser.

## Run with PowerShell

```powershell
cd path\to\tracevision
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:5000. If PowerShell blocks activation, run `Set-ExecutionPolicy -Scope Process Bypass` first, or just use `run.bat`.

## Typical workflow

1. Live Camera -> **Start camera** (allow webcam access if Windows asks).
2. Turn on **Motion scanning**, then move in front of the camera. A MOTION CANDIDATE notice appears and an event is stored. The cooldown (Settings) limits repeats.
3. Security Events -> **Acknowledge**, **Mark reviewed**, **Verify as real** (only a person can do this) or **Dismiss**.
4. **Create incident** from an acknowledged alert, add notes, and resolve it with a summary.
5. Audit Log -> **Verify audit chain**. Event History -> **Export CSV**.

Data is kept in `data\tracevision.db` and survives restarts. To start fresh, close the app and delete that file.

## Run the tests

```powershell
python -m unittest discover -s tests -t . -v
```
Tests use a temporary database and a fake camera. They do not need a webcam. For the physical webcam, follow `MANUAL_TEST_CHECKLIST.md`.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Could not open camera 0" | Close Zoom/Teams/Camera app. Windows Settings -> Privacy -> Camera -> allow desktop apps. Try camera index 1 in Settings. |
| `python` not found | Reinstall Python with "Add to PATH" ticked. |
| Port 5000 in use | Close the other program, or change `port=5000` at the bottom of `app.py`. |
| Too many or too few motion events | Adjust Sensitivity and Cooldown in Settings. |
| Black or frozen feed | Stop and start the camera. The camera pill shows "stalled" if frames stop for 5 seconds. |

## Layout

`app.py` API and server - `camera.py` webcam thread and motion detector - `db.py` SQLite, validation, audit chain - `templates/`, `static/` browser UI - `tests/` automated tests.

## Audit log limits

Each audit entry stores a hash of the previous entry, so edits or deletions in the middle are detected. It is **not tamper-proof**: someone with file access can drop the newest entries or rewrite the whole chain.

## Known limitations

- Single local webcam. Authentication is not implemented.
- Motion detection is basic. Moving shadows, screens and curtains can trigger it. A fixed camera works best.
