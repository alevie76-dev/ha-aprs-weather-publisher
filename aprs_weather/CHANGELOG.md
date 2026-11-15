# Changelog

## 1.1.0

- Add APRS rainfall fields `r` (last 60 minutes) and `p` (last 24 hours), alongside the existing `P` (since midnight).
- Rolling totals are reconstructed from `sensor.davis_vantage_rain_day`, and the history persists across restarts in `/data/rain_history.json`. Midnight resets, daylight-saving changes, outages and counter glitches are handled. Unknown values are omitted, never reported as zero.
- New optional settings `rain_1h_entity` / `rain_24h_entity` let the app use rolling-total sensors directly instead of reconstructing them.
- If the daily rain sensor is unavailable, the rain fields are now omitted and the rest of the weather report is still sent. Previously the whole report was skipped.
- No changes to callsign, APRS-IS login, server, interval or existing configuration.

## 0.1.0

- Initial release.
