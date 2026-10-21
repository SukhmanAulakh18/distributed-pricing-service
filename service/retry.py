import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")


def with_retry(fn: Callable[[], T], attempts: int = 3, base_delay: float = 0.05,
               max_delay: float = 1.0, retry_on: tuple = (Exception,)) -> T:
    """Call fn, retrying with exponential backoff and full jitter."""
    for attempt in range(attempts):
        try:
            return fn()
        except retry_on:
            if attempt == attempts - 1:
                raise
            delay = min(max_delay, base_delay * 2 ** attempt)
            time.sleep(random.uniform(0, delay))
    raise RuntimeError("unreachable")
