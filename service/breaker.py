import logging
import threading
import time

from prometheus_client import Gauge

log = logging.getLogger(__name__)

# 0 closed, 1 half-open, 2 open. The triage tool and the dashboard read this.
STATE = Gauge("dependency_circuit_state", "Circuit breaker state (0 closed, 1 half-open, 2 open)",
              ["dependency"])
_LEVEL = {"closed": 0, "half_open": 1, "open": 2}


class CircuitOpen(Exception):
    pass


class CircuitBreaker:
    """Fail fast after repeated failures instead of making every request wait
    out timeouts and retries against a store that is down.

    closed: calls go through. After `threshold` consecutive failures -> open.
    open: calls fail immediately for `cooldown_s`. Then -> half-open.
    half-open: one trial call goes through; success closes, failure re-opens.
    """

    def __init__(self, name: str = "dependency", threshold: int = 5, cooldown_s: float = 10.0):
        self.name = name
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.failures = 0
        self.opened_at: float | None = None
        self.trial_in_flight = False
        self.lock = threading.Lock()
        STATE.labels(name).set(0)

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "open" if time.monotonic() - self.opened_at < self.cooldown_s else "half_open"

    def call(self, fn):
        with self.lock:
            st = self.state
            if st == "open" or (st == "half_open" and self.trial_in_flight):
                raise CircuitOpen(f"{self.name} circuit open")
            if st == "half_open":
                self.trial_in_flight = True
                STATE.labels(self.name).set(_LEVEL["half_open"])
        try:
            result = fn()
        except Exception:
            with self.lock:
                self.trial_in_flight = False
                self.failures += 1
                if self.opened_at is not None or self.failures >= self.threshold:
                    if self.opened_at is None:
                        log.error("circuit opened", extra={"dependency": self.name, "failures": self.failures})
                    self.opened_at = time.monotonic()
                    STATE.labels(self.name).set(_LEVEL["open"])
            raise
        with self.lock:
            if self.opened_at is not None:
                log.info("circuit closed", extra={"dependency": self.name})
            self.failures, self.opened_at, self.trial_in_flight = 0, None, False
            STATE.labels(self.name).set(0)
        return result
