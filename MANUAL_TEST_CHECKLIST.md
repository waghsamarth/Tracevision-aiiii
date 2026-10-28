# Manual webcam checklist (physical laptop, Windows)

Mark each item yourself. These were NOT run by the developer tooling.

- [ ] `run.bat` starts without errors and the browser opens the dashboard.
- [ ] Startup overlay shows "Backend responded" and then fades away.
- [ ] Live Camera -> Start camera. Webcam light turns on, real video appears, LIVE and timestamp show.
- [ ] Click Start camera again (or press it quickly twice): no second camera handle, no crash.
- [ ] Turn on Motion scanning. Sit still for 10 s: no events.
- [ ] Wave your hand: green frame outline, MOTION CANDIDATE notice, one new event in Security Events. Source reads "Real motion candidate".
- [ ] Keep waving: no new event until the cooldown passes.
- [ ] Raise Sensitivity to 10 and lower it to 1: behaviour changes as expected.
- [ ] Turn the room light on/off: no burst of false events.
- [ ] Acknowledge -> Mark reviewed -> Verify as real -> Create incident. Incident shows "Human verified".
- [ ] Add a note, resolve (summary required), close. Audit Log shows each step. Verify audit chain says intact.
- [ ] Stop camera: webcam light turns off and the feed disappears.
- [ ] Start camera, then unplug a USB webcam (if you have one): camera pill changes and a camera error event appears.
- [ ] Open the Camera app first, then Start camera in TraceVision: a clear "could not open" message and demo-mode placeholder.
- [ ] Close the console window: webcam light turns off. Reopen: events and incidents are still there.
- [ ] Resize the window to a narrow width: navigation and panels stay usable.
- [ ] Enable "Reduce motion" in Windows: scan line and fades stop.
