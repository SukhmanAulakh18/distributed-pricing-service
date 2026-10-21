"""Screenshots of the demo UIs (Rancher, Grafana, Jaeger, Alertmanager) with
headless Chrome over the DevTools protocol.

Rancher's self-signed certificate is accepted (local only), and its session
cookie is set from an API token, so no password is typed into a page.

  python scripts/capture.py rancher-nodes=https://localhost:8443/dashboard/c/<id>/explorer/node \\
      grafana-slo=http://localhost:3001/d/pricing-slo --out docs/img/ui --rancher-token deploy/rancher/.api-token
"""
import argparse
import base64
import itertools
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request

import websocket

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class Tab:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, suppress_origin=True)
        self.ids = itertools.count(1)

    def call(self, method: str, **params):
        msg_id = next(self.ids)
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pages", nargs="+", help="name=url")
    ap.add_argument("--out", default="docs/img/ui")
    ap.add_argument("--rancher-token")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--wait", type=float, default=8.0)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    with socket.socket() as sock:  # any free port, so captures can run side by side
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    profile = tempfile.mkdtemp(prefix="capture-chrome-")
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={port}", "--remote-allow-origins=*",
                               "--ignore-certificate-errors", f"--user-data-dir={profile}", "--hide-scrollbars",
                               "--no-first-run", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
        tab = Tab(json.load(urllib.request.urlopen(req))["webSocketDebuggerUrl"])
        tab.call("Page.enable")
        tab.call("Network.enable")
        tab.call("Emulation.setDeviceMetricsOverride", width=a.width, height=a.height, deviceScaleFactor=1, mobile=False)
        if a.rancher_token:
            token = open(a.rancher_token).read().strip()
            tab.call("Network.setCookie", name="R_SESS", value=token, domain="localhost", path="/", secure=True, httpOnly=True)
        for page in a.pages:
            name, url = page.split("=", 1)
            tab.call("Page.navigate", url=url)
            time.sleep(a.wait)
            png = base64.b64decode(tab.call("Page.captureScreenshot", format="png")["data"])
            path = os.path.join(a.out, f"{name}.png")
            with open(path, "wb") as f:
                f.write(png)
            print(path, len(png))
    finally:
        chrome.terminate()


if __name__ == "__main__":
    main()
