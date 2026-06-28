# Diagnostics

## Scripts
- `auto_execute_bot.py`: Runs the real-window bot with preflight checks.
- `active_window_probe.py`: Verifies capture and optional OCR against the target window.
- `manual_capture_probe.py`: Verifies capture and click delivery against the target window.
- `manual_matcher_workbench.py`: Template matching diagnostics.
- `manual_ocr_probe.py`: OCR diagnostics.
- `decision_demo.py`: Decision engine demo.
- `phase4_sanity_probe.py`: Runtime sanity checks.

## Config Semantics
- `window.capture_mode`: capture strategy only.
- `input.mode`: click/input strategy only.
- `input.mode: foreground`: always use foreground clicks.
- `input.mode: auto`: try background clicks first, then fallback to foreground.

## Notes
- Automated tests live under `tests/`.
- Manual diagnostics should follow the same `input.mode` as the main bot unless explicitly overridden.
- Windows live-window automation is `admin-only`.
- Launch the game and the bot with the same privilege level.
- Use windowed or borderless-windowed mode; exclusive fullscreen is not a supported baseline.
