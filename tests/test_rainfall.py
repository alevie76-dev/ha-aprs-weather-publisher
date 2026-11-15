"""Tests for rolling rainfall reporting. Run: python3 -m unittest discover -s tests -v

No network access: Home Assistant is simulated and the APRS-IS socket is
patched to fail loudly if anything attempts to connect.
"""
import datetime as dt
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aprs_weather"))
import publisher  # noqa: E402
import rainfall  # noqa: E402

DEN = ZoneInfo("America/Denver")  # Helena, MT
STEP = 300  # production reporting interval
ENTITY = publisher.RAIN_DAY_SENSOR


def local(*args, fold=0):
    return dt.datetime(*args, tzinfo=DEN, fold=fold).timestamp()


def daily_counter(events, console_offset=0):
    """Simulate the Davis rain-day counter: rain since (console) local midnight.

    ``events`` is a list of (epoch, inches). ``console_offset`` shifts the
    console's midnight relative to Home Assistant's clock (seconds).
    """
    def value(t):
        console_now = dt.datetime.fromtimestamp(t - console_offset, DEN)
        midnight = console_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + console_offset
        return round(sum(a for et, a in events if midnight < et <= t), 4)
    return value


def feed(history, start, end, counter, step=STEP, skip=()):
    t = start
    while t <= end:
        if not any(a <= t < b for a, b in skip):
            history.add(t, counter(t))
        t += step


def new_history(path="/nonexistent/rain_history.json"):
    return rainfall.RainHistory(path, ENTITY, tz=DEN)


def entity(entity_id, state, unit=None):
    attrs = {"unit_of_measurement": unit} if unit else {}
    return {"entity_id": entity_id, "state": str(state), "attributes": attrs}


def ha_entities(observed, **overrides):
    values = {
        "temperature": entity("sensor.davis_vantage_temperature", 41.3, "°F"),
        "humidity": entity("sensor.davis_vantage_humidity", 87, "%"),
        "direction": entity("sensor.davis_vantage_wind_direction_average", 225, "°"),
        "speed": entity("sensor.davis_vantage_wind_speed_average", 7.4, "mph"),
        "gust": entity("sensor.davis_vantage_wind_gust", 15, "mph"),
        "pressure": entity("sensor.davis_vantage_barometric_pressure", 29.92, "inHg"),
        "last_success": entity("sensor.davis_vantage_last_success_time", observed.isoformat()),
    }
    values.update(overrides)
    return values


OPTS = {
    "callsign": "KH7AL-13", "aprs_passcode": "", "latitude": 46.5891, "longitude": -112.0391,
    "dry_run": True, "interval_seconds": 300, "aprs_server": "noam.aprs2.net", "aprs_port": 14580,
}
PACKET_RE = re.compile(
    r"^KH7AL-13>APRS,TCPIP\*:!\d{4}\.\d{2}[NS]/\d{5}\.\d{2}[EW]_"
    r"\d{3}/\d{3}g\d{3}t-?\d{2,3}(?P<rain>(?:r\d{3})?(?:p\d{3})?(?:P\d{3})?)h\d{2}b\d{5}$"
)


class NoRainfall(unittest.TestCase):
    def test_dry_day_reports_zero_only_with_full_history(self):
        h = new_history()
        now = local(2026, 11, 15, 10, 0)
        feed(h, now - 26 * 3600, now, daily_counter([]))
        self.assertEqual(h.total(rainfall.HOUR, now), 0.0)
        self.assertEqual(h.total(rainfall.DAY, now), 0.0)


class SixtyMinuteRain(unittest.TestCase):
    def test_last_hour_excludes_earlier_rain(self):
        now = local(2026, 11, 15, 14, 0)
        events = [(local(2026, 11, 15, 11, 3), 0.20),  # 3 h ago: only in p
                  (local(2026, 11, 15, 13, 7), 0.05), (local(2026, 11, 15, 13, 22), 0.10),
                  (local(2026, 11, 15, 13, 51), 0.03)]
        h = new_history()
        feed(h, now - 25 * 3600, now, daily_counter(events))
        self.assertAlmostEqual(h.total(rainfall.HOUR, now), 0.18)
        self.assertAlmostEqual(h.total(rainfall.DAY, now), 0.38)

    def test_insufficient_history_is_unknown_not_zero(self):
        h = new_history()
        now = local(2026, 11, 15, 14, 0)
        feed(h, now - 40 * 60, now, daily_counter([]))
        self.assertIsNone(h.total(rainfall.HOUR, now))
        self.assertIsNone(h.total(rainfall.DAY, now))
        feed(h, now + STEP, now + 25 * 60, daily_counter([]))
        self.assertEqual(h.total(rainfall.HOUR, now + 25 * 60), 0.0)  # 65 min now
        self.assertIsNone(h.total(rainfall.DAY, now + 25 * 60))


class TwentyFourHourRain(unittest.TestCase):
    def test_spans_yesterday_and_today(self):
        now = local(2026, 11, 15, 10, 0)
        events = [(local(2026, 11, 14, 8, 30), 0.25),   # 25.5 h ago: excluded
                  (local(2026, 11, 14, 15, 12), 0.40),  # yesterday afternoon
                  (local(2026, 11, 15, 6, 44), 0.10)]
        counter = daily_counter(events)
        h = new_history()
        feed(h, now - 26 * 3600, now, counter)
        self.assertAlmostEqual(h.total(rainfall.DAY, now), 0.50)
        self.assertAlmostEqual(counter(now), 0.10)  # P is independent of p


class MidnightReset(unittest.TestCase):
    def setUp(self):
        self.events = [(local(2026, 11, 14, 18, 2), 0.35), (local(2026, 11, 14, 23, 47), 0.10),
                       (local(2026, 11, 15, 0, 12), 0.20)]
        self.now = local(2026, 11, 15, 0, 45)

    def check(self, counter):
        h = new_history()
        feed(h, self.now - 26 * 3600, self.now, counter)
        self.assertAlmostEqual(h.total(rainfall.HOUR, self.now), 0.30)
        self.assertAlmostEqual(h.total(rainfall.DAY, self.now), 0.65)
        self.assertAlmostEqual(counter(self.now), 0.20)

    def test_reset_is_not_negative_or_counted_as_rain(self):
        self.check(daily_counter(self.events))

    def test_console_clock_slightly_behind(self):
        self.check(daily_counter(self.events, console_offset=7 * 60))

    def test_console_clock_slightly_ahead(self):
        self.check(daily_counter(self.events, console_offset=-6 * 60))


class DaylightSaving(unittest.TestCase):
    def test_fall_back_25_hour_day(self):
        # DST ends 2026-11-01 02:00 MDT; 01:30 occurs twice.
        events = [(local(2026, 10, 31, 21, 30), 0.07),  # outside window
                  (local(2026, 10, 31, 22, 30), 0.10),
                  (local(2026, 11, 1, 1, 30, fold=0), 0.05),
                  (local(2026, 11, 1, 1, 30, fold=1), 0.05),
                  (local(2026, 11, 1, 12, 0), 0.20)]
        counter = daily_counter(events)
        now = local(2026, 11, 1, 21, 0)  # 24 h elapsed ago = 2026-10-31 22:00 MDT
        h = new_history()
        feed(h, now - 26 * 3600, now, counter)
        self.assertAlmostEqual(h.total(rainfall.DAY, now), 0.40)
        self.assertAlmostEqual(counter(now), 0.30)
        # One elapsed hour across the repeated hour.
        later = local(2026, 11, 1, 1, 50, fold=1)
        self.assertAlmostEqual(h.total(rainfall.HOUR, later), 0.05)

    def test_spring_forward_23_hour_day(self):
        # DST starts 2026-03-08 02:00 MST -> 03:00 MDT.
        events = [(local(2026, 3, 7, 23, 30), 0.04),  # outside window
                  (local(2026, 3, 8, 1, 55), 0.10), (local(2026, 3, 8, 3, 5), 0.10),
                  (local(2026, 3, 8, 23, 50), 0.05), (local(2026, 3, 9, 0, 20), 0.05)]
        counter = daily_counter(events)
        h = new_history()
        start = local(2026, 3, 6, 22, 0)
        now = local(2026, 3, 9, 1, 0)
        feed(h, start, now, counter)
        self.assertAlmostEqual(h.total(rainfall.DAY, now), 0.30)
        # 01:55 MST and 03:05 MDT are 10 minutes apart in elapsed time.
        self.assertAlmostEqual(h.total(rainfall.HOUR, local(2026, 3, 8, 3, 30)), 0.20)

    def test_mid_night_drop_on_dst_day_is_not_a_midnight_reset(self):
        h = new_history()
        t = local(2026, 11, 1, 1, 0)
        for i, v in enumerate([0.20, 0.20, 0.00, 0.00, 0.00, 0.00]):
            h.add(t + i * STEP, v)
        self.assertIsNone(h.total(rainfall.HOUR, t + 5 * STEP))


class RestartPersistence(unittest.TestCase):
    def test_history_survives_restart(self):
        now = local(2026, 11, 15, 14, 0)
        events = [(local(2026, 11, 15, 13, 31), 0.12), (local(2026, 11, 14, 20, 0), 0.30)]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rain_history.json")
            h = new_history(path)
            feed(h, now - 26 * 3600, now, daily_counter(events))
            h.save()
            h2 = new_history(path)
            h2.load(now=now)
            self.assertEqual(h2.samples, h.samples)
            self.assertAlmostEqual(h2.total(rainfall.HOUR, now), 0.12)
            self.assertAlmostEqual(h2.total(rainfall.DAY, now), 0.42)

    def test_downtime_gap_same_day(self):
        events = [(local(2026, 11, 15, 10, 20), 0.05)]  # falls while app is down
        counter = daily_counter(events)
        h = new_history()
        feed(h, local(2026, 11, 14, 9, 0), local(2026, 11, 15, 12, 0), counter,
             skip=[(local(2026, 11, 15, 10, 1), local(2026, 11, 15, 10, 40))])
        # Gap straddles the 1 h window start and contained rain: unknown.
        self.assertIsNone(h.total(rainfall.HOUR, local(2026, 11, 15, 11, 30)))
        # Gap fully inside the window: counter difference is exact.
        self.assertAlmostEqual(h.total(rainfall.DAY, local(2026, 11, 15, 11, 30)), 0.05)
        self.assertEqual(h.total(rainfall.HOUR, local(2026, 11, 15, 11, 45)), 0.0)

    def test_downtime_gap_across_midnight_is_unknown(self):
        h = new_history()
        feed(h, local(2026, 11, 14, 8, 0), local(2026, 11, 15, 9, 0), daily_counter([]),
             skip=[(local(2026, 11, 14, 23, 30), local(2026, 11, 15, 1, 0))])
        now = local(2026, 11, 15, 9, 0)
        self.assertEqual(h.total(rainfall.HOUR, now), 0.0)
        self.assertIsNone(h.total(rainfall.DAY, now))

    def test_corrupt_or_foreign_history_starts_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rain_history.json")
            with open(path, "w") as f:
                f.write("{not json")
            h = new_history(path)
            h.load()
            self.assertEqual(h.samples, [])
            with open(path, "w") as f:
                json.dump({"version": 1, "entity": "sensor.other", "samples": [[1, 0.1]]}, f)
            h.load()
            self.assertEqual(h.samples, [])


class MissingAndStale(unittest.TestCase):
    def test_no_current_reading(self):
        h = new_history()
        now = local(2026, 11, 15, 14, 0)
        feed(h, now - 26 * 3600, now - 30 * 60, daily_counter([]))
        self.assertIsNone(h.total(rainfall.HOUR, now))
        self.assertIsNone(h.total(rainfall.DAY, now))

    def test_duplicate_and_out_of_order_readings_ignored(self):
        h = new_history()
        self.assertTrue(h.add(1000.0, 0.1))
        self.assertFalse(h.add(1000.0, 0.1))
        self.assertFalse(h.add(900.0, 0.0))
        self.assertEqual(h.samples, [[1000.0, 0.1]])
        with self.assertRaises(ValueError):
            h.add(1300.0, float("nan"))


class UnexpectedReset(unittest.TestCase):
    def test_midday_reset_is_never_rain(self):
        h = new_history()
        start = local(2026, 11, 15, 12, 0)
        values = [0.40] * 25 + [0.00] * 30  # reset at 14:05, stays reset
        for i, v in enumerate(values):
            h.add(start + i * STEP, v)
        t_reset = start + 25 * STEP
        self.assertIsNone(h.total(rainfall.HOUR, t_reset + 30 * 60))
        self.assertEqual(h.total(rainfall.HOUR, t_reset + 75 * 60), 0.0)

    def test_single_glitch_reading_is_not_rain(self):
        h = new_history()
        start = local(2026, 11, 15, 12, 0)
        values = [0.40] * 25 + [0.00] + [0.40] * 30
        for i, v in enumerate(values):
            h.add(start + i * STEP, v)
        t_glitch = start + 25 * STEP
        for minutes in range(5, 120, 5):
            total = h.total(rainfall.HOUR, t_glitch + minutes * 60)
            self.assertIn(total, (None, 0.0), minutes)
        self.assertEqual(h.total(rainfall.HOUR, t_glitch + 70 * 60), 0.0)

    def test_implausible_jump_is_not_rain(self):
        h = new_history()
        start = local(2026, 11, 15, 12, 0)
        values = [0.10] * 20 + [7.50] + [0.10] * 20
        for i, v in enumerate(values):
            h.add(start + i * STEP, v)
        for i in range(20, 41):
            self.assertIn(h.total(rainfall.HOUR, start + i * STEP), (None, 0.0))


class PacketFormatting(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime.now(dt.timezone.utc)

    def build(self, rain):
        return publisher.packet(OPTS, ha_entities(self.now), rain)

    def test_field_order_and_units(self):
        line = self.build({"r": 0.18, "p": 0.5, "P": 0.1})
        self.assertRegex(line, PACKET_RE)
        self.assertIn("_225/007g015t041r018p050P010h87b10132", line)

    def test_unknown_fields_omitted_not_zero(self):
        line = self.build({"r": None, "p": None, "P": 0.0})
        self.assertIn("t041P000h87", line)
        self.assertNotIn("r000", line)
        self.assertNotIn("p000", line)
        line = self.build({"r": 0.04, "p": None, "P": 0.12})
        self.assertIn("t041r004P012h87", line)

    def test_all_rain_unknown(self):
        line = self.build({"r": None, "p": None, "P": None})
        self.assertIn("t041h87b", line)
        self.assertRegex(line, PACKET_RE)

    def test_out_of_aprs_range_omitted(self):
        line = self.build({"r": 0.5, "p": 10.25, "P": 9.99})
        self.assertIn("r050P999h", line)

    def test_legacy_call_without_rain_argument(self):
        ents = ha_entities(self.now, rain_day=entity(ENTITY, 0.23, "in"))
        self.assertIn("t041P023h87", publisher.packet(OPTS, ents))


class DryRun(unittest.TestCase):
    """Full cycle with simulated Home Assistant; any socket connection fails the test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "rain_history.json")
        self.observed = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cycle(self, states, opts=OPTS, history=None):
        def fake_get(entity_id, token):
            if entity_id not in states:
                raise OSError("HTTP Error 404: Not Found")
            return states[entity_id]
        history = history or rainfall.RainHistory(self.path, ENTITY)
        with mock.patch.object(publisher, "get_entity", side_effect=fake_get), \
             mock.patch.object(publisher, "local_timezone", return_value=DEN), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network used")), \
             self.assertLogs("aprs_weather", "INFO") as logs:
            line = publisher.run_cycle(opts, "test-token", history)
        return line, "\n".join(logs.output), history

    def states(self, rain_day="0.14", **extra):
        s = {e["entity_id"]: e for e in ha_entities(self.observed).values()}
        if rain_day is not None:
            s[ENTITY] = entity(ENTITY, rain_day, "in")
        s.update(extra)
        return s

    def test_dry_run_first_start_with_existing_options(self):
        line, logs, _ = self.run_cycle(self.states())
        self.assertRegex(line, PACKET_RE)
        self.assertIn("t041P014h87", line)  # no history yet: r/p omitted
        self.assertIn("DRY RUN (not transmitted): " + line, logs)
        self.assertIn("last 60 min (r) unavailable (omitted)", logs)
        with open(self.path) as f:
            saved = json.load(f)
        self.assertEqual(saved["entity"], ENTITY)
        self.assertEqual(saved["samples"][-1][1], 0.14)

    def test_dry_run_with_persisted_history(self):
        h = rainfall.RainHistory(self.path, ENTITY, tz=DEN)
        now = self.observed.timestamp()
        for i in range(26 * 12, 0, -1):
            h.add(now - i * STEP, 0.14 if i < 6 else 0.10)  # 0.04" in the last 30 min
        h.save()
        h2 = rainfall.RainHistory(self.path, ENTITY)
        h2.load()
        line, logs, _ = self.run_cycle(self.states(), history=h2)
        self.assertRegex(line, PACKET_RE)
        self.assertIn("t041r004p004P014h87", line)
        self.assertIn("DRY RUN (not transmitted)", logs)

    def test_dry_run_rain_sensor_unavailable(self):
        line, logs, h = self.run_cycle(self.states(rain_day="unavailable"))
        self.assertIn("t041h87b", line)
        self.assertNotRegex(line, r"[rpP]\d{3}")
        self.assertIn("Daily rainfall unavailable", logs)
        self.assertEqual(h.samples, [])

    def test_stale_observation_skips_report(self):
        stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)).isoformat()
        states = self.states()
        states["sensor.davis_vantage_last_success_time"] = entity("sensor.davis_vantage_last_success_time", stale)
        h = rainfall.RainHistory(self.path, ENTITY)
        with mock.patch.object(publisher, "get_entity", side_effect=lambda e, t: states[e]), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network used")):
            with self.assertRaisesRegex(ValueError, "stale"):
                publisher.run_cycle(OPTS, "test-token", h)
        self.assertEqual(h.samples, [])

    def test_configured_rolling_sensors_preferred(self):
        opts = dict(OPTS, rain_1h_entity="sensor.davis_vantage_rain_last_hour",
                    rain_24h_entity="sensor.davis_vantage_rain_last_24h")
        states = self.states(**{
            "sensor.davis_vantage_rain_last_hour": entity("sensor.davis_vantage_rain_last_hour", 0.03, "in"),
            "sensor.davis_vantage_rain_last_24h": entity("sensor.davis_vantage_rain_last_24h", 0.61, "in"),
        })
        line, logs, _ = self.run_cycle(states, opts)
        self.assertIn("t041r003p061P014h87", line)
        self.assertIn("[sensor.davis_vantage_rain_last_hour]", logs)

    def test_bad_configured_sensor_falls_back(self):
        opts = dict(OPTS, rain_1h_entity="sensor.missing", rain_24h_entity="../core/api/states")
        line, logs, _ = self.run_cycle(self.states(), opts)
        self.assertIn("t041P014h87", line)
        self.assertIn("using reconstructed total", logs)


if __name__ == "__main__":
    unittest.main()
