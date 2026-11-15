# HA APRS Weather Publisher

A Home Assistant app repository for publishing Davis Vantage Vue weather observations via APRS-IS as `KH7AL-13`.

**Version 1.1.2: 1-hour (`r`) and 24-hour (`p`) rainfall, calm-wind and outage handling. Dry-run by default.**

## Installation

1. In Home Assistant, open **Settings → Apps → Install app**.
2. Open the top-right menu and select **Repositories**.
3. Add `https://github.com/alevie76-dev/ha-aprs-weather-publisher`.
4. Refresh the app store, open **APRS-IS Weather Publisher**, and install.
5. Configure the station's latitude/longitude and start in dry-run mode.
6. Review the **Logs** before enabling APRS-IS transmissions.

See [app documentation](aprs_weather/DOCS.md) for safety notes and configuration.

## Rainfall (1.1.0)

Packets include `r` (rain in the last 60 minutes), `p` (rain in the last 24 hours) and `P` (rain since local midnight), all in hundredths of an inch. `r` and `p` are reconstructed from the Davis daily-rain counter, and that history persists across restarts. `r` starts appearing about 1 hour after installing or upgrading, and `p` about 24 hours after. Until then they are omitted rather than sent as zero. See [DOCS.md](aprs_weather/DOCS.md#rainfall-reporting-v110) for details, optional sensors and upgrade steps.

## Development

Run the tests (stdlib only, no network):

```
python3 -m unittest discover -s tests -v
```
