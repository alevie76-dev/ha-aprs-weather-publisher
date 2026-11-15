# APRS-IS Weather Publisher — setup

1. Install the app from the repository, then open its **Configuration** tab.
2. Set your station's accurate **latitude** and **longitude** (negative longitude in Montana). These are publicly transmitted in APRS packets. You can round them if you want less precision.
3. Keep `dry_run: true`. Leave `aprs_passcode` blank for now.
4. Start the app and inspect **Logs** for `DRY RUN (not transmitted):` and a correctly formatted weather packet.
5. Verify the pressure value is reasonable. The configured Davis `barometric_pressure` sensor is assumed to represent **sea-level-adjusted pressure**; the separate `sea_level_pressure` entity was excluded because its observed reading was implausible.
6. Confirm the wind direction/speed and gust fields match your sensor semantics. The integration's `wind_gust` value has not been independently confirmed to be a 5-minute peak.
7. Obtain your APRS-IS passcode using a trusted method. Never put the passcode in the public GitHub repository.
8. Enter the passcode in the app's local configuration and change `dry_run` to `false` only after reviewing the test packet. Restart the app.

## Packet contents

- Position: APRS latitude/longitude with weather symbol `_`.
- Wind direction: `sensor.davis_vantage_wind_direction_average` (degrees).
- Wind speed: `sensor.davis_vantage_wind_speed_average` (mph).
- Gust: `sensor.davis_vantage_wind_gust` (mph).
- Temperature: `sensor.davis_vantage_temperature` (F).
- Humidity: `sensor.davis_vantage_humidity` (%).
- Pressure: `sensor.davis_vantage_barometric_pressure` (inHg, converted to tenths of hPa).
- Daily rain: `sensor.davis_vantage_rain_day` (inches since midnight, `P`).
- 1-hour/24-hour rainfall (`r` and `p`) are **not** transmitted; rolling totals need to be implemented separately.
- Stale readings older than 15 minutes are not transmitted.

## Security

The app accesses Home Assistant via Supervisor-provided API credentials. It does **not** need a Home Assistant long-lived access token. The APRS passcode is stored in Home Assistant's local app options, not in GitHub. No inbound ports are opened.

## Important limitations

The app does not yet independently verify the barometric sensor's sea-level adjustment or the gust's reporting window. Validate these before live publishing. No RF is transmitted: this is APRS-IS only. APRS station position is publicly visible.
