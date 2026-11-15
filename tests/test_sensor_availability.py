"""Regression tests for sensor availability and Davis timestamp handling.

Covers the production errors:
  "Report skipped: Sensor unavailable: sensor.davis_vantage_wind_direction_average"
  "Report skipped: Invalid Davis last-success timestamp"
No network access: Home Assistant is simulated and APRS-IS sockets fail loudly.
"""
import datetime as dt
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from test_rainfall import DEN, ENTITY, OPTS, PACKET_RE, entity, ha_entities
import publisher
import rainfall

DIRECTION = "sensor.davis_vantage_wind_direction_average"
LAST = "sensor.davis_vantage_last_success_time"


def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def with_last(state):
    return ha_entities(now_utc(), last_success=entity(LAST, state))


class WindDirection(unittest.TestCase):
    def build(self, direction_state, speed=7.4):
        ents = ha_entities(now_utc(),
                           direction=entity(DIRECTION, direction_state, "°"),
                           speed=entity("sensor.davis_vantage_wind_speed_average", speed, "mph"))
        return publisher.packet(OPTS, ents, {"P": 0.0})

    def test_calm_air_unknown_direction_uses_aprs_missing_value(self):
        with self.assertLogs("aprs_weather", "INFO") as logs:
            line = self.build("unknown", speed=0)
        self.assertRegex(line, PACKET_RE)
        self.assertIn("_.../000g015t041", line)
        self.assertIn("undefined in calm air", "\n".join(logs.output))

    def test_unavailable_direction_while_windy_is_missing_not_zero(self):
        for state in ("unavailable", "unknown", "none", ""):
            line = self.build(state)
            self.assertIn("_.../007g015", line, state)
            self.assertNotIn("_000/", line)

    def test_valid_direction_unchanged(self):
        self.assertIn("_225/007g015", self.build(225))
        self.assertIn("_000/007g015", self.build(360))
        self.assertIn("_000/007g015", self.build(0))

    def test_other_core_sensor_unavailable_still_skips_report(self):
        ents = ha_entities(now_utc(), temperature=entity("sensor.davis_vantage_temperature", "unavailable", "°F"))
        with self.assertRaises(publisher.TemporaryDataError):
            publisher.packet(OPTS, ents, {})

    def test_direction_misconfiguration_is_not_hidden(self):
        wrong_unit = ha_entities(now_utc(), direction=entity(DIRECTION, 225, "rad"))
        with self.assertRaises(publisher.ConfigurationError):
            publisher.packet(OPTS, wrong_unit, {})
        garbage = ha_entities(now_utc(), direction=entity(DIRECTION, "SW", "°"))
        with self.assertRaises(publisher.ConfigurationError):
            publisher.packet(OPTS, garbage, {})
        out_of_range = ha_entities(now_utc(), direction=entity(DIRECTION, 400, "°"))
        with self.assertRaisesRegex(ValueError, "Invalid wind direction"):
            publisher.packet(OPTS, out_of_range, {})


class LastSuccessTimestamp(unittest.TestCase):
    def test_valid_home_assistant_formats(self):
        t = now_utc() - dt.timedelta(seconds=40)
        formats = [
            t.isoformat(),                                         # 2026-11-15T20:31:05+00:00
            t.replace(microsecond=123456).isoformat(),             # fractional seconds
            t.strftime("%Y-%m-%dT%H:%M:%SZ"),                      # UTC Z
            t.strftime("%Y-%m-%dT%H:%M:%Sz"),
            t.astimezone(DEN).isoformat(),                         # -07:00 / -06:00 offset
            t.isoformat(sep=" "),                                  # space separator
            "  " + t.isoformat() + " ",                            # stray whitespace
        ]
        for value in formats:
            observed = publisher.observation_time(with_last(value))
            self.assertAlmostEqual(observed.timestamp(), t.timestamp(), delta=1, msg=value)

    def test_no_reading_yet_is_temporary(self):
        for state in ("unknown", "unavailable", "", "None", None):
            ents = with_last(state)
            if state is None:
                ents["last_success"]["state"] = None
            with self.assertRaisesRegex(publisher.TemporaryDataError, "no current reading"):
                publisher.observation_time(ents)

    def test_malformed_timestamps_are_configuration_errors(self):
        naive = now_utc().replace(tzinfo=None).isoformat()
        for value in ("garbage", "15/11/2026 13:31", "1763238665", naive, "2026-13-45T99:00:00+00:00"):
            with self.assertRaises(publisher.ConfigurationError, msg=value):
                publisher.observation_time(with_last(value))

    def test_stale_and_future_timestamps_rejected(self):
        for delta in (dt.timedelta(minutes=16), dt.timedelta(hours=5), dt.timedelta(minutes=-5)):
            value = (now_utc() - delta).isoformat()
            with self.assertRaisesRegex(publisher.TemporaryDataError, "stale or clock incorrect", msg=str(delta)):
                publisher.observation_time(with_last(value))

    def test_max_age_boundary_preserved(self):
        publisher.observation_time(with_last((now_utc() - dt.timedelta(seconds=890)).isoformat()))
        publisher.observation_time(with_last((now_utc() + dt.timedelta(seconds=100)).isoformat()))
        with self.assertRaises(publisher.TemporaryDataError):
            publisher.observation_time(with_last((now_utc() - dt.timedelta(seconds=910)).isoformat()))


class EntityFetchErrors(unittest.TestCase):
    def fetch_with(self, exc):
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            publisher.get_entity(DIRECTION, "t")

    def test_missing_entity_is_configuration_error(self):
        err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with self.assertRaisesRegex(publisher.ConfigurationError, "not found"):
            self.fetch_with(err)

    def test_auth_failure_is_configuration_error(self):
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with self.assertRaises(publisher.ConfigurationError):
            self.fetch_with(err)

    def test_home_assistant_restarting_is_temporary(self):
        for exc in (urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None),
                    urllib.error.URLError(ConnectionRefusedError("refused"))):
            with self.assertRaises(publisher.TemporaryDataError):
                self.fetch_with(exc)


class RecoveryAndMainLoop(unittest.TestCase):
    """Full cycles with simulated Home Assistant; any socket connection fails the test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = rainfall.RainHistory(os.path.join(self.tmp.name, "rain_history.json"), ENTITY)

    def tearDown(self):
        self.tmp.cleanup()

    def states(self, direction=225, last=None, rain_day="0.14"):
        s = {e["entity_id"]: e for e in ha_entities(now_utc()).values()}
        s[DIRECTION] = entity(DIRECTION, direction, "°")
        if last is not None:
            s[LAST] = entity(LAST, last)
        if rain_day is not None:
            s[ENTITY] = entity(ENTITY, rain_day, "in")
        return s

    def cycle(self, states):
        def fake_get(entity_id, token):
            if entity_id not in states:
                raise publisher.ConfigurationError(f"Entity {entity_id} not found")
            return states[entity_id]
        with mock.patch.object(publisher, "get_entity", side_effect=fake_get), \
             mock.patch.object(publisher, "local_timezone", return_value=DEN), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network used")):
            return publisher.run_cycle(OPTS, "test-token", self.history)

    def test_direction_recovers_on_next_report(self):
        self.assertIn("_.../007", self.cycle(self.states(direction="unknown")))
        self.assertIn("_225/007", self.cycle(self.states(direction=225)))

    def test_last_success_recovers_on_next_report(self):
        with self.assertRaises(publisher.TemporaryDataError):
            self.cycle(self.states(last="unavailable"))
        self.assertEqual(self.history.samples, [])  # no observation, nothing recorded
        line = self.cycle(self.states())
        self.assertRegex(line, PACKET_RE)
        self.assertEqual(len(self.history.samples), 1)

    def test_rain_and_direction_problems_together_still_report(self):
        states = self.states(direction="unknown", rain_day="unavailable")
        line = self.cycle(states)
        self.assertIn("_.../007g015t041h87b10132", line)

    def test_timezone_failure_cannot_block_report(self):
        def fake_get(entity_id, token):
            return self.states()[entity_id]
        with mock.patch.object(publisher, "get_entity", side_effect=fake_get), \
             mock.patch.object(publisher, "local_timezone", side_effect=RuntimeError("boom")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network used")), \
             self.assertLogs("aprs_weather", "WARNING"):
            line = publisher.run_cycle(OPTS, "test-token", self.history)
        self.assertIn("_225/007g015t041h87b10132", line)

    def run_main_once(self, states):
        """One iteration of main(); time.sleep ends the loop."""
        options = os.path.join(self.tmp.name, "options.json")
        with open(options, "w") as f:
            json.dump(OPTS, f)

        def fake_get(entity_id, token):
            if entity_id not in states:
                raise publisher.ConfigurationError(f"Entity {entity_id} not found in Home Assistant")
            return states[entity_id]
        with mock.patch.object(publisher, "OPTIONS", options), \
             mock.patch.object(publisher, "RAIN_HISTORY", os.path.join(self.tmp.name, "h.json")), \
             mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "test-token"}), \
             mock.patch.object(publisher, "get_entity", side_effect=fake_get), \
             mock.patch.object(publisher, "local_timezone", return_value=DEN), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network used")), \
             mock.patch.object(publisher.time, "sleep", side_effect=KeyboardInterrupt), \
             self.assertLogs("aprs_weather", "INFO") as logs:
            with self.assertRaises(KeyboardInterrupt):
                publisher.main()
        return logs.output

    def test_main_logs_outage_as_temporary_warning(self):
        out = self.run_main_once(self.states(last="unknown"))
        self.assertTrue(any(l.startswith("WARNING:aprs_weather:Report skipped (temporary, will retry)") for l in out), out)

    def test_main_logs_missing_entity_as_configuration_error(self):
        states = self.states()
        del states[DIRECTION]
        out = self.run_main_once(states)
        self.assertTrue(any(l.startswith("ERROR:aprs_weather:Report skipped (configuration problem)") for l in out), out)

    def test_main_dry_run_with_calm_wind(self):
        out = self.run_main_once(self.states(direction="unknown"))
        self.assertTrue(any("DRY RUN (not transmitted)" in l and "_.../007" in l for l in out), out)


if __name__ == "__main__":
    unittest.main()
