"""Home Assistant sensor -> APRS-IS weather publisher. Stdlib only."""
import datetime as dt
import json
import logging
import math
import os
import re
import socket
import time
import urllib.error
import urllib.request
import zoneinfo

import rainfall

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("aprs_weather")
OPTIONS = "/data/options.json"
RAIN_HISTORY = "/data/rain_history.json"
SENSORS = {
    "temperature": "sensor.davis_vantage_temperature",
    "humidity": "sensor.davis_vantage_humidity",
    "direction": "sensor.davis_vantage_wind_direction_average",
    "speed": "sensor.davis_vantage_wind_speed_average",
    "gust": "sensor.davis_vantage_wind_gust",
    "pressure": "sensor.davis_vantage_barometric_pressure",
    "last_success": "sensor.davis_vantage_last_success_time",
}
# Fetched separately so a rain-sensor problem omits only the rain fields.
RAIN_DAY_SENSOR = "sensor.davis_vantage_rain_day"
# Optional directly-reported rolling totals (inches): APRS field -> option name.
ROLLING_RAIN_OPTIONS = {"r": "rain_1h_entity", "p": "rain_24h_entity"}
ENTITY_ID = re.compile(r"sensor\.[a-z0-9_]+")


# Home Assistant states meaning "no current value" (outage, startup, calm wind).
NO_DATA_STATES = ("unknown", "unavailable", "none", "")


class TemporaryDataError(ValueError):
    """Data is temporarily missing or stale; expected to recover on its own."""


class ConfigurationError(ValueError):
    """A configured entity is missing or reports malformed data; needs attention."""


def get_entity(entity_id, token):
    request = urllib.request.Request(
        "http://supervisor/core/api/states/" + entity_id,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ConfigurationError(
                f"Entity {entity_id} not found in Home Assistant; check the Davis integration entity IDs"
            ) from exc
        if exc.code in (401, 403):
            raise ConfigurationError(f"Home Assistant API refused access (HTTP {exc.code}); check homeassistant_api") from exc
        raise TemporaryDataError(f"Home Assistant API error for {entity_id} (HTTP {exc.code})") from exc
    except urllib.error.URLError as exc:
        raise TemporaryDataError(f"Home Assistant API unreachable ({exc.reason})") from exc


def local_timezone(token):
    """Home Assistant's configured time zone, falling back to the TZ variable."""
    name = None
    try:
        request = urllib.request.Request(
            "http://supervisor/core/api/config",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            name = json.load(response).get("time_zone")
    except Exception as exc:
        LOG.warning("Could not read Home Assistant time zone (%s)", exc)
    name = name or os.environ.get("TZ")
    try:
        return zoneinfo.ZoneInfo(name) if name else None
    except Exception:
        LOG.warning("Unknown time zone %r; midnight resets cannot be identified, so rolling totals may be omitted", name)
        return None


def numeric(entity, expected_unit=None):
    if entity["state"] in NO_DATA_STATES:
        raise TemporaryDataError("Sensor unavailable: " + entity["entity_id"])
    unit = entity.get("attributes", {}).get("unit_of_measurement")
    if expected_unit and unit != expected_unit:
        raise ConfigurationError(f"Unexpected unit for {entity['entity_id']}: {unit!r} (expected {expected_unit!r})")
    try:
        result = float(entity["state"])
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"Non-numeric state for {entity['entity_id']}: {str(entity['state'])[:40]!r}") from exc
    if not math.isfinite(result):
        raise ValueError("Nonfinite sensor: " + entity["entity_id"])
    return result


def position(lat, lon):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Invalid latitude/longitude")
    def coord(value, is_lat):
        degrees = int(abs(value))
        minutes = (abs(value) - degrees) * 60
        if minutes >= 59.995:
            degrees += 1
            minutes = 0.0
        return (f"{degrees:02d}{minutes:05.2f}" if is_lat else f"{degrees:03d}{minutes:05.2f}") + (
            ("N" if value >= 0 else "S") if is_lat else ("E" if value >= 0 else "W")
        )
    return coord(lat, True) + "/" + coord(lon, False) + "_"


def observation_time(entities):
    """Time of the Davis observation; rejects missing, malformed and stale timestamps.

    Accepts ISO 8601 with a UTC offset or ``Z`` (Home Assistant serializes
    timestamp sensors as e.g. ``2026-11-15T20:31:05.123456+00:00``).
    """
    entity = entities["last_success"]
    last = entity.get("state")
    if last is None or str(last).strip().lower() in NO_DATA_STATES:
        raise TemporaryDataError(
            f"Davis last-success time is {last!r}: the integration has no current reading "
            "(Home Assistant starting up or console unreachable)"
        )
    text = str(last).strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"  # also supports Python < 3.11
    try:
        timestamp = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConfigurationError(
            f"Invalid Davis last-success timestamp {text[:40]!r}; expected ISO 8601 with a time zone. "
            f"Check that {entity.get('entity_id')} is the integration's last-success timestamp sensor"
        ) from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ConfigurationError(f"Davis last-success timestamp {text[:40]!r} has no time zone; cannot determine its age")
    age = (dt.datetime.now(dt.timezone.utc) - timestamp).total_seconds()
    if age < -120 or age > 900:
        raise TemporaryDataError(f"Davis observations stale or clock incorrect (age {age:.0f}s)")
    return timestamp


def rain_day_value(entity):
    """Inches since midnight from the daily counter, or raise ValueError."""
    value = numeric(entity, "in")
    if not (0 <= value <= rainfall.MAX_DAILY_IN):
        raise ValueError(f"Implausible daily rain {value}")
    return value


def rain_field(letter, inches):
    """APRS rain field in hundredths of an inch; empty (omitted) if unknown or too large."""
    if inches is None:
        return ""
    hundredths = round(inches * 100)
    if not 0 <= hundredths <= 999:
        LOG.warning("Rain field %s value %.2f in exceeds APRS range; omitted", letter, inches)
        return ""
    return f"{letter}{hundredths:03d}"


def packet(opts, entities, rain=None):
    """Build the APRS packet. ``rain`` maps r/p/P to inches, or None to omit."""
    observation_time(entities)
    temp = numeric(entities["temperature"], "°F")
    humidity = numeric(entities["humidity"], "%")
    speed = numeric(entities["speed"], "mph")
    try:
        direction = numeric(entities["direction"], "°")
    except TemporaryDataError:
        # The Davis integration leaves average direction unset in calm air (and
        # when the vane reports no direction). APRS represents unknown wind
        # direction as "..."; never invent a direction or send 000.
        direction = None
        LOG.info("Wind direction %s; sending APRS missing direction '...'",
                 "undefined in calm air" if speed == 0 else "temporarily unavailable")
    gust = numeric(entities["gust"], "mph")
    pressure_inhg = numeric(entities["pressure"], "inHg")
    if rain is None:
        rain = {"P": rain_day_value(entities["rain_day"])} if "rain_day" in entities else {}
    if not (-99 <= temp <= 140 and 0 <= humidity <= 100 and 0 <= speed <= 999 and 0 <= gust <= 999):
        raise ValueError("Temperature, humidity or wind reading outside expected range")
    if not (25 <= pressure_inhg <= 32):
        raise ValueError("Pressure outside plausible sea-level inHg range; verify Davis sensor mapping")
    if direction is not None and not (0 <= direction <= 360):
        raise ValueError("Invalid wind direction")
    direction_text = "..." if direction is None else f"{round(direction) % 360:03d}"
    h = 0 if round(humidity) == 100 else round(humidity)
    pressure_tenths = round(pressure_inhg * 33.8638866667 * 10)
    # APRS order: r (last 60 min), p (last 24 h), P (since midnight),
    # hundredths of an inch. Unknown values are omitted, never sent as zero.
    weather = (
        f"{direction_text}/{round(speed):03d}"
        f"g{round(gust):03d}t{round(temp):03d}"
        f"{rain_field('r', rain.get('r'))}{rain_field('p', rain.get('p'))}{rain_field('P', rain.get('P'))}"
        f"h{h:02d}b{pressure_tenths:05d}"
    )
    source = opts["callsign"].upper().strip()
    if not re.fullmatch(r"[A-Z0-9]{1,6}(?:-[0-9]{1,2})?", source):
        raise ValueError("Invalid APRS callsign")
    return f"{source}>APRS,TCPIP*:!{position(float(opts['latitude']), float(opts['longitude']))}{weather}"


def publish(opts, line):
    passcode = str(opts.get("aprs_passcode", "")).strip()
    if not re.fullmatch(r"\d{1,5}", passcode):
        raise ValueError("Set a valid APRS-IS numeric passcode in the app configuration")
    host = opts["aprs_server"]
    with socket.create_connection((host, int(opts["aprs_port"])), timeout=20) as conn:
        conn.settimeout(20)
        conn.sendall(f"user {opts['callsign']} pass {passcode} vers HA-APRS-Weather 0.1\r\n".encode("ascii"))
        response = b""
        # APRS servers commonly send a banner before the # logresp line.
        # Wait for explicit verification, not merely the first newline.
        while len(response) < 8192:
            data = conn.recv(4096)
            if not data:
                raise ConnectionError("APRS-IS disconnected during login")
            response += data
            greeting = response.decode("ascii", errors="replace")
            if re.search(r"^# logresp\s+\S+\s+unverified(?:\s|,|$)", greeting, re.I | re.M):
                raise PermissionError("APRS-IS rejected passcode; no packet sent")
            if re.search(r"^# logresp\s+\S+\s+verified(?:\s|,|$)", greeting, re.I | re.M):
                break
        else:
            raise PermissionError("APRS-IS did not confirm verified login; no packet sent")
        conn.sendall((line + "\r\n").encode("ascii"))
        LOG.info("Sent APRS-IS weather packet for %s", opts["callsign"])


def _describe(inches):
    return "unavailable (omitted)" if inches is None else f"{inches:.2f} in"


def collect_rain(opts, token, history, observed):
    """Return {"r", "p", "P"} in inches (None = omit) and update history."""
    rain = {"r": None, "p": None, "P": None}
    sources = {"r": "reconstructed", "p": "reconstructed", "P": RAIN_DAY_SENSOR}
    now = observed.timestamp()
    try:
        rain["P"] = rain_day_value(get_entity(RAIN_DAY_SENSOR, token))
    except Exception as exc:
        LOG.warning("Daily rainfall unavailable (%s); rainfall fields omitted this report", exc)
    if rain["P"] is not None:
        history.add(now, rain["P"])
        history.save()
        rain["r"] = history.total(rainfall.HOUR, now)
        rain["p"] = history.total(rainfall.DAY, now)
    for field, option in ROLLING_RAIN_OPTIONS.items():
        entity_id = str(opts.get(option) or "").strip()
        if not entity_id:
            continue
        try:
            if not ENTITY_ID.fullmatch(entity_id):
                raise ValueError("not a sensor entity id")
            value = numeric(get_entity(entity_id, token), "in")
            if value < 0:
                raise ValueError("negative rainfall")
            rain[field], sources[field] = value, entity_id
        except Exception as exc:
            LOG.warning("Configured %s %r unusable (%s); using reconstructed total", option, entity_id, exc)
    LOG.info(
        "Rainfall: since midnight (P) %s; last 60 min (r) %s [%s]; last 24 h (p) %s [%s]; history %.1f h",
        _describe(rain["P"]), _describe(rain["r"]), sources["r"], _describe(rain["p"]), sources["p"],
        history.coverage_hours(now),
    )
    return rain


def run_cycle(opts, token, history):
    """Fetch sensors, build one packet, and log or transmit it. Returns the packet."""
    entities = {key: get_entity(value, token) for key, value in SENSORS.items()}
    observed = observation_time(entities)
    try:
        if history.tz is None:
            history.tz = local_timezone(token)
        rain = collect_rain(opts, token, history, observed)
    except Exception as exc:
        # Rainfall must never block an otherwise valid weather report.
        LOG.warning("Rainfall processing failed (%s); rainfall fields omitted this report", exc)
        rain = {"r": None, "p": None, "P": None}
    line = packet(opts, entities, rain)
    if opts.get("dry_run", True):
        LOG.info("DRY RUN (not transmitted): %s", line)
    else:
        publish(opts, line)
    return line


def main():
    history = None
    while True:
        interval = 300
        try:
            with open(OPTIONS, encoding="utf-8") as f:
                opts = json.load(f)
            interval = max(60, int(opts.get("interval_seconds", 300)))
            lat, lon = float(opts["latitude"]), float(opts["longitude"])
            if lat == 0 and lon == 0:
                raise ValueError("Set station latitude and longitude in app configuration")
            token = os.environ.get("SUPERVISOR_TOKEN")
            if not token:
                raise RuntimeError("SUPERVISOR_TOKEN unavailable; check homeassistant_api setting")
            if history is None:
                history = rainfall.RainHistory(RAIN_HISTORY, RAIN_DAY_SENSOR)
                try:
                    history.load()
                except Exception as exc:
                    LOG.warning("Rainfall history could not be loaded (%s); starting fresh", exc)
                    history.samples = []
            run_cycle(opts, token, history)
        except TemporaryDataError as exc:
            LOG.warning("Report skipped (temporary, will retry): %s", exc)
        except ConfigurationError as exc:
            LOG.error("Report skipped (configuration problem): %s", exc)
        except Exception as exc:
            LOG.error("Report skipped: %s", exc)
        time.sleep(interval)


if __name__ == "__main__":
    main()
