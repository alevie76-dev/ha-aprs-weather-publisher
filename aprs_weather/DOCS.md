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
- Wind direction: `sensor.davis_vantage_wind_direction_average` (degrees). The Davis integration leaves this sensor `unknown` in calm air (average speed 0) and when the vane reports no direction. The packet then carries the standard APRS missing direction `...` (e.g. `_.../000g003…`), never an invented or zero direction.
- Log levels: a temporary outage (sensor `unknown`/`unavailable`, Home Assistant restarting, stale data) logs `WARNING Report skipped (temporary, will retry)`. A missing entity, wrong unit or malformed value logs `ERROR Report skipped (configuration problem)`, which needs your attention.
- Wind speed: `sensor.davis_vantage_wind_speed_average` (mph).
- Gust: `sensor.davis_vantage_wind_gust` (mph).
- Temperature: `sensor.davis_vantage_temperature` (F).
- Humidity: `sensor.davis_vantage_humidity` (%).
- Pressure: `sensor.davis_vantage_barometric_pressure` (inHg, converted to tenths of hPa).
- Rainfall: `r`, `p` and `P` (see **Rainfall reporting** below).
- Stale readings older than 15 minutes are not transmitted.

Fields appear in standard APRS order: `ddd/sss gGGG tTTT rRRR pRRR PRRR hHH bBBBBB`.

## Rainfall reporting (v1.1.0)

| Field | Meaning | Source |
|---|---|---|
| `r` | Rain in the preceding 60 minutes | `rain_1h_entity` if configured, otherwise reconstructed |
| `p` | Rain in the preceding 24 hours | `rain_24h_entity` if configured, otherwise reconstructed |
| `P` | Rain since local midnight | `sensor.davis_vantage_rain_day` (unchanged) |

All three are hundredths of an inch (`P012` = 0.12 in). **A field is left out of the packet, never sent as zero, when its value is unknown.** For example, a packet may contain `P012` with no `r` or `p`.

### Sensors

- **Required (already in use):** `sensor.davis_vantage_rain_day`, in inches. `P` comes straight from it, and the rolling totals are reconstructed from it.
- **Not used:** `sensor.davis_vantage_rain_rate`. It is an instantaneous rate, not an accumulated amount.
- **Optional:** `rain_1h_entity` and `rain_24h_entity`. Set these only if your Home Assistant has sensors that already report rolling 1-hour or 24-hour rainfall totals in inches. Davis consoles compute these internally (LOOP2 data), but whether your Davis integration exposes them depends on its version. To check, open **Developer Tools → States** and search for `rain`. Do not use storm, month, year or rate sensors. If a configured sensor is unavailable, unknown or not in inches, the reconstructed total is used and a warning is logged. Both options are blank by default.

### How reconstructed totals work

Every report, the app records the daily rain counter with the Davis observation time, saving to `/data/rain_history.json` (the app's persistent storage, about 26 hours of readings). Rain between two readings is the difference between them. `r` and `p` add up those differences over the window.

- **Midnight reset:** a counter drop within 20 minutes of local midnight is the normal daily reset. Only the new reading (rain since the reset) is counted, and the drop itself is never counted. Midnight is determined in Home Assistant's configured time zone, so daylight-saving changes are handled. Windows are measured in elapsed time, so "24 hours" is always 24 real hours, even on 23- or 25-hour days.
- **Unexpected drop or jump** (counter reset mid-day, glitch reading, implausible spike): that interval is treated as unknown, never as rain. `r` or `p` is omitted until the interval leaves the window, up to 1 or 24 hours. A single bad reading that returns to the prior value is not mistaken for rain.
- **Restarts and outages:** history is reloaded on start. A gap within the same day is exact, because the counter difference still holds. A gap longer than 15 minutes that may cross midnight makes the affected windows unknown, since rain before the reset could have been lost.
- **Missing or unavailable rain sensor:** `r`, `p` and `P` are omitted from that report, and the rest of the packet is still sent.

### History has to build up first

After first installing or upgrading, `r` appears after about **1 hour** and `p` after about **24 hours**. Until then, those fields are omitted. `P` is reported immediately, as before.

### Accuracy notes

- Readings are 5 minutes apart, so a window can start up to one interval early. Rain in the interval straddling the window start is counted in full, which matches common weather-software practice.
- Rain that falls in the few minutes between the last reading before midnight and the console's reset can be missed. Rain is never invented to fill that gap.
- Reconstructed `r` and `p` need `interval_seconds` of 900 (15 minutes) or less. The default is 300. With longer intervals, any interval that crosses midnight, or that straddles a window start while it is raining, can't be attributed, so `r`/`p` are usually omitted. `P` is unaffected.
- History is pruned to the last 26 hours. That keeps the reading at or before the 24-hour window start, and comes to about 313 readings (~8 KB) at the default interval.
- Values of 10.00 in or more cannot be encoded in APRS's three digits and are omitted.

### Verifying in dry-run mode

With `dry_run: true`, each report logs a line like:

```
Rainfall: since midnight (P) 0.12 in; last 60 min (r) 0.03 in [reconstructed]; last 24 h (p) unavailable (omitted) [reconstructed]; history 3.4 h
DRY RUN (not transmitted): KH7AL-13>APRS,TCPIP*:!....._225/007g015t041r003P012h87b10132
```

Compare `P` with `sensor.davis_vantage_rain_day`, and compare `r`/`p` with the console's own 1-hour/24-hour rain display or with the sensor's history graph. Rainfall history is recorded in dry-run mode too, so the totals are ready when you switch to live.

## Upgrading from 0.1.0

1. **Settings → Apps → APRS-IS Weather Publisher**. If the update doesn't show, open the app store's top-right menu and choose **Check for updates**.
2. Select **Update**. The app is rebuilt from the repository. Your existing configuration (callsign, passcode, position, `dry_run`, server, interval) is kept as-is, and no changes are required.
3. Optional: to use direct rolling-total sensors, enable **Show unused optional configuration options** on the Configuration tab and set `rain_1h_entity` / `rain_24h_entity`.
4. Check **Logs** for the `Rainfall:` line and a packet that still contains your usual wind, temperature, `P`, humidity and pressure values.

## Security

The app accesses Home Assistant via Supervisor-provided API credentials. It does **not** need a Home Assistant long-lived access token. The APRS passcode is stored in Home Assistant's local app options, not in GitHub. No inbound ports are opened.

## Important limitations

The app does not independently verify the barometric sensor's sea-level adjustment or the gust's reporting window. Validate these before live publishing. No RF is transmitted: this is APRS-IS only. APRS station position is publicly visible.
