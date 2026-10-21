import hashlib
import json
import logging
import time
import uuid

import redis

from .breaker import CircuitBreaker, CircuitOpen
from .models import PriceRequest

log = logging.getLogger(__name__)

KEY_PREFIX = "price:v1:"


def request_hash(req: PriceRequest) -> str:
    """SHA-256 of what determines the price: product, simulation settings and
    market-data version (canonical JSON, sorted keys).

    The instrument id is left out, so every trade on the same contract shares
    one cache entry and one price. A new market-data version gives new keys,
    so old results are never served as current.
    """
    key = {"product": req.product.model_dump(), "md": req.market_data_version}
    if req.product.steps:  # closed-form prices do not depend on the simulation
        key["simulation"] = req.simulation.model_dump()
    canonical = json.dumps(key, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def seed_from_hash(h: str) -> int:
    return int(h[:16], 16)


class PriceCache:
    """Redis cache that degrades to a no-op when Redis is unavailable.

    Every method swallows Redis errors: a cache outage should cost latency,
    not availability. A circuit breaker keeps that latency cost small: once
    Redis has failed a few times in a row, calls skip it entirely instead of
    each waiting out a socket timeout.
    """

    def __init__(self, client: redis.Redis | None, ttl_seconds: int = 30,
                 lock_ttl_ms: int = 5_000, breaker: CircuitBreaker | None = None):
        self.client = client
        self.ttl = ttl_seconds
        self.lock_ttl_ms = lock_ttl_ms
        self.breaker = breaker or CircuitBreaker("redis", threshold=3, cooldown_s=5)

    def _call(self, fn):
        return self.breaker.call(fn)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def get(self, h: str) -> dict | None:
        return self.get_many([h])[0]

    def get_many(self, hashes: list[str]) -> list[dict | None]:
        """One MGET round trip for a whole batch of lookups."""
        if not self.client or not hashes:
            return [None] * len(hashes)
        try:
            raws = self._call(lambda: self.client.mget([KEY_PREFIX + h for h in hashes]))
            return [json.loads(r) if r else None for r in raws]
        except CircuitOpen:
            return [None] * len(hashes)
        except redis.RedisError as e:
            log.warning("cache get failed: %s", e)
            return [None] * len(hashes)

    def set(self, h: str, value: dict) -> None:
        self.set_many({h: value})

    def set_many(self, values: dict[str, dict]) -> None:
        """Pipelined SET ... EX: one round trip for the batch."""
        if not self.client or not values:
            return

        def write():
            pipe = self.client.pipeline(transaction=False)
            for h, v in values.items():
                pipe.set(KEY_PREFIX + h, json.dumps(v), ex=self.ttl)
            pipe.execute()

        try:
            self._call(write)
        except CircuitOpen:
            pass
        except redis.RedisError as e:
            log.warning("cache set failed: %s", e)

    def acquire_lock(self, h: str) -> str | None:
        """SET NX lock so only one worker computes a given miss.

        Returns a token on success. Returns "" when Redis is down, which
        callers treat as "go ahead and compute".
        """
        if not self.client:
            return ""
        token = uuid.uuid4().hex
        try:
            ok = self._call(lambda: self.client.set(KEY_PREFIX + h + ":lock", token, nx=True,
                                                    px=self.lock_ttl_ms))
            return token if ok else None
        except CircuitOpen:
            return ""
        except redis.RedisError as e:
            log.warning("cache lock failed: %s", e)
            return ""

    _RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"

    def release_lock(self, h: str, token: str) -> None:
        if not self.client or not token:
            return
        try:
            # Compare-and-delete so a slow worker never removes a lock that
            # expired and was re-acquired by someone else.
            self._call(lambda: self.client.eval(self._RELEASE, 1, KEY_PREFIX + h + ":lock", token))
        except CircuitOpen:
            pass
        except redis.RedisError as e:
            log.warning("cache unlock failed: %s", e)

    def wait_for(self, h: str, timeout_s: float, poll_s: float = 0.025) -> dict | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.breaker.state != "closed":
                return None  # Redis went away mid-wait; stop waiting and compute.
            hit = self.get(h)
            if hit is not None:
                return hit
            time.sleep(poll_s)
        return None

    def ping(self) -> bool:
        if not self.client:
            return True
        try:
            return bool(self.client.ping())
        except redis.RedisError:
            return False
