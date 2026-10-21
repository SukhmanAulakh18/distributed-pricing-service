"""Small helpers to reconfigure the compose stack between benchmark runs."""
import os
import subprocess
import time

import httpx

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMPOSE = ["docker", "compose", "-f", os.path.join(ROOT, "deploy", "docker-compose.yml")]
API = "http://localhost:8000"


def up(workers: int, **env) -> None:
    """Scales workers (and recreates API + workers if env changes) and waits for readiness."""
    e = {**os.environ, **{k: str(v) for k, v in env.items()}}
    cmd = COMPOSE + ["up", "-d", "--scale", f"worker={workers}"]
    if env:
        cmd.append("--force-recreate")
    subprocess.run(cmd + ["api", "worker"], check=True, capture_output=True, env=e)
    for _ in range(120):
        try:
            if httpx.get(f"{API}/health/ready", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    time.sleep(3)  # let workers join the consumer group


def worker_ids() -> list[str]:
    return subprocess.check_output(COMPOSE + ["ps", "-q", "worker"], text=True).split()
