"""Rolling rainfall totals reconstructed from a cumulative daily rain counter.

The Davis console's "rain day" counter accumulates rainfall since local
midnight and resets to zero at midnight. Rainfall during any interval between
two timestamped readings is therefore the difference between them, provided
the counter did not reset in between. This module stores those readings,
persists them across restarts, and sums the per-interval increments over a
rolling window.

Design rules (see DOCS.md):
* A total is returned only when every interval in the window is known.
  Otherwise ``None`` is returned and the APRS field is omitted; zero is never
  assumed.
* A counter decrease near local midnight is the normal daily reset. The new
  reading (rain since the reset) is counted; the decrease itself is not.
* A counter decrease at any other time is an unexpected reset or glitch. That
  interval is unknown, so windows containing it are unavailable until the
  interval ages out of the window.
* Long gaps (restarts, outages) are fine within a single day, because the
  counter difference is still exact. A long gap that may contain a midnight
  reset is unknown, because rain before the reset could have been lost.

Stdlib only.
"""
import datetime as dt
import json
import logging
import math
import os
import tempfile

LOG = logging.getLogger("aprs_weather")

HOUR = 3600
DAY = 24 * HOUR
FORMAT_VERSION = 1
# Readings older than this are discarded (24 h window plus margin).
RETENTION_SECONDS = DAY + 2 * HOUR
# Longest interval whose rainfall may be attributed across a window edge or
# across a midnight reset. Normal readings arrive every 5 minutes.
MAX_ATTRIBUTION_SECONDS = 15 * 60
# The console clock and Home Assistant clock may disagree slightly, so a
# counter decrease within this distance of local midnight is a daily reset.
MIDNIGHT_TOLERANCE_SECONDS = 20 * 60
# Decreases smaller than this are treated as rounding noise, not resets.
NOISE_IN = 0.005
# An increase larger than MAX_STEP_IN + MAX_RATE_IN_PER_HOUR * hours is
# treated as a counter discontinuity rather than rainfall.
MAX_STEP_IN = 0.50
MAX_RATE_IN_PER_HOUR = 6.0
# Consecutive inconsistent readings needed before accepting a counter reset.
SUSPECT_LIMIT = 3
# Sanity bound on the daily counter itself.
MAX_DAILY_IN = 50.0


class RainHistory:
    """Timestamped daily-rain readings plus rolling-total calculation."""

    def __init__(self, path, source_entity, tz=None):
        self.path = path
        self.source_entity = source_entity
        self.tz = tz
        self.samples = []  # sorted [[epoch_seconds, inches], ...]

    # ---- persistence -------------------------------------------------
    def load(self, now=None):
        """Load persisted readings. A missing or invalid file starts empty."""
        self.samples = []
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            LOG.info("No rainfall history yet; rolling totals will become available as readings accumulate")
            return
        except (OSError, ValueError) as exc:
            LOG.warning("Rainfall history unreadable (%s); starting fresh", exc)
            return
        if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
            LOG.warning("Rainfall history has unknown format; starting fresh")
            return
        if data.get("entity") != self.source_entity:
            LOG.warning("Rainfall history was recorded from a different sensor; starting fresh")
            return
        cleaned = {}
        for item in data.get("samples", []):
            try:
                t, v = float(item[0]), float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if math.isfinite(t) and math.isfinite(v) and 0 <= v <= MAX_DAILY_IN:
                cleaned[t] = v
        self.samples = [[t, cleaned[t]] for t in sorted(cleaned)]
        now = dt.datetime.now(dt.timezone.utc).timestamp() if now is None else now
        # Drop readings from the future (clock was wrong) and expired ones.
        self.samples = [s for s in self.samples if s[0] <= now + 120]
        self._prune(now)
        LOG.info("Loaded %d rainfall readings covering %.1f h", len(self.samples), self.coverage_hours(now))

    def save(self):
        """Atomically write readings. Failure is logged, never fatal."""
        payload = {"version": FORMAT_VERSION, "entity": self.source_entity, "samples": self.samples}
        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            fd, tmp = tempfile.mkstemp(prefix=".rain_history.", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, separators=(",", ":"))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            LOG.warning("Could not save rainfall history (%s); totals may be unavailable after restart", exc)

    # ---- recording ---------------------------------------------------
    def add(self, when, inches):
        """Record a reading. ``when`` is epoch seconds of the observation."""
        if not (math.isfinite(when) and math.isfinite(inches) and 0 <= inches <= MAX_DAILY_IN):
            raise ValueError("Invalid rainfall reading")
        inches = round(inches, 4)
        if self.samples:
            last_t = self.samples[-1][0]
            if abs(when - last_t) < 1:
                return False  # same observation already recorded
            if when < last_t:
                LOG.warning("Rainfall reading older than the last recorded one; ignored")
                return False
        self.samples.append([when, inches])
        self._prune(when)
        return True

    def _prune(self, now):
        cutoff = now - RETENTION_SECONDS
        self.samples = [s for s in self.samples if s[0] >= cutoff]

    def coverage_hours(self, now):
        if not self.samples:
            return 0.0
        return max(0.0, (now - self.samples[0][0]) / HOUR)

    # ---- calculation -------------------------------------------------
    def _local_date(self, t):
        return dt.datetime.fromtimestamp(t, dt.timezone.utc).astimezone(self.tz).date()

    def _may_contain_midnight(self, t0, t1):
        """True if a daily reset could fall inside [t0, t1] (with clock tolerance)."""
        if self.tz is None:
            return True  # cannot rule it out
        return self._local_date(t0 - MIDNIGHT_TOLERANCE_SECONDS) != self._local_date(t1 + MIDNIGHT_TOLERANCE_SECONDS)

    def increments(self):
        """Rain (inches) for each interval samples[i-1] -> samples[i]; None = unknown.

        Readings are compared with the last *trusted* counter value, so an
        isolated bad reading (e.g. 0.40 -> 0.00 -> 0.40) is never mistaken for
        a reset followed by rain. A reading inconsistent with the trusted value
        is "suspect"; after SUSPECT_LIMIT consecutive suspect readings the
        counter is accepted as genuinely reset. Intervals involving suspect
        readings are unknown.
        """
        result = [None]
        if not self.samples:
            return []
        ref_t, ref_v = self.samples[0]
        suspect = 0
        for i in range(1, len(self.samples)):
            t0 = self.samples[i - 1][0]
            t1, v1 = self.samples[i]
            near_midnight = self._may_contain_midnight(t0, t1)
            if t1 - t0 > MAX_ATTRIBUTION_SECONDS and near_midnight:
                # A reset may have occurred inside a long gap; rain before it is unknown.
                result.append(None)
                ref_t, ref_v, suspect = t1, v1, 0
                continue
            delta = v1 - ref_v
            if delta < -NOISE_IN and near_midnight and self.tz is not None:
                delta = v1  # normal daily reset: count only rain since the reset
            elif delta < -NOISE_IN:
                delta = None  # unexpected reset or glitch: never counted as rain
            else:
                delta = max(delta, 0.0)
                if delta > MAX_STEP_IN + MAX_RATE_IN_PER_HOUR * (t1 - ref_t) / HOUR:
                    delta = None  # implausible jump: counter discontinuity
            if delta is None:
                suspect += 1
                result.append(None)
                if suspect >= SUSPECT_LIMIT:
                    ref_t, ref_v, suspect = t1, v1, 0  # accept the new counter level
                continue
            # First consistent reading after suspect ones: rain during the
            # suspect period cannot be placed in time, so only zero is known.
            result.append(delta if suspect == 0 or delta == 0 else None)
            ref_t, ref_v, suspect = t1, v1, 0
        return result

    def total(self, window_seconds, now):
        """Rainfall over the ``window_seconds`` ending at ``now``, or None if unknown.

        The baseline is the latest reading at or before the window start. An
        interval straddling the start is counted in full (the window may begin
        up to MAX_ATTRIBUTION_SECONDS early), or is zero, or makes the total
        unknown if it is longer than that and contains rain.
        """
        samples = self.samples
        if not samples or samples[-1][0] < now - MAX_ATTRIBUTION_SECONDS:
            return None  # no current reading
        start = now - window_seconds
        base = None
        for i, (t, _) in enumerate(samples):
            if t <= start:
                base = i
            else:
                break
        if base is None:
            return None  # insufficient history
        incs = self.increments()
        total = 0.0
        for i in range(base, len(samples) - 1):
            a, b = samples[i], samples[i + 1]
            if b[0] > now:
                break
            inc = incs[i + 1]
            if inc is None:
                return None
            if a[0] < start and inc > 0 and b[0] - a[0] > MAX_ATTRIBUTION_SECONDS:
                return None  # cannot tell how much of this rain fell inside the window
            total += inc
        return round(total, 4)
