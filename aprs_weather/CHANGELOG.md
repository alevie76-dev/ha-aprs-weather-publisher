# Changelog

## 1.1.1

Reliability fixes for rainfall reporting. APRS-IS login, connection, reporting interval and the other weather fields are unchanged.

- Fix: if Home Assistant returned a time zone that isn't text, every weather report could be skipped. The app now logs a warning and leaves out only the rolling rain totals.
- Fix: a damaged rainfall history file (a reading list that isn't a list) no longer skips a report at startup. The app starts with a fresh history instead.
- Any unexpected rainfall error now omits only the rain fields for that report, and the rest of the weather packet is still sent.
- Docs: rebuilt `r`/`p` need `interval_seconds` of 900 or less (default 300), and history is kept for 26 hours.
- Added tests for history size limits, loading damaged history files, time zone failures and configuration compatibility (36 tests).

## 1.1.0

- Add APRS rainfall fields `r` (last 60 minutes) and `p` (last 24 hours), alongside the existing `P` (since midnight).
- Rolling totals are reconstructed from `sensor.davis_vantage_rain_day`, and the history persists across restarts in `/data/rain_history.json`. Midnight resets, daylight-saving changes, outages and counter glitches are handled. Unknown values are omitted, never reported as zero.
- New optional settings `rain_1h_entity` / `rain_24h_entity` let the app use rolling-total sensors directly instead of reconstructing them.
- If the daily rain sensor is unavailable, the rain fields are now omitted and the rest of the weather report is still sent. Previously the whole report was skipped.
- No changes to callsign, APRS-IS login, server, interval or existing configuration.

## 0.1.0

- Initial release.
