"""Builds docs/img/demo.gif from screenshots captured during a chaos run:
the Grafana cluster dashboard, an Alertmanager inset, and a caption naming
the latest fault, so the GIF shows the cluster being hit and healing.

Run: python scripts/make_gif.py bench/results/frames bench/results/chaos-run2.json bench/results/frames/frames.log
"""
import datetime
import glob
import json
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 820
BG, INK, ACCENT = (14, 17, 19), (231, 234, 231), (92, 192, 180)


def font(size):
    for path in ("/System/Library/Fonts/SFNSMono.ttf", "/System/Library/Fonts/Menlo.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def main(frames_dir, chaos_json, frames_log):
    run = json.load(open(chaos_json))
    start = run["started_at"]
    times = {}
    for line in open(frames_log):
        m = re.match(r"frame (\d+) at (\d+):(\d+):(\d+)", line)
        if m:
            day = datetime.datetime.fromtimestamp(start)
            t = day.replace(hour=int(m[2]), minute=int(m[3]), second=int(m[4])).timestamp()
            times[m[1]] = t - start
    big, small = font(26), font(18)
    out = []
    for g in sorted(glob.glob(os.path.join(frames_dir, "f*-grafana.png"))):
        n = os.path.basename(g)[1:3]
        alerts = g.replace("-grafana.png", "-alerts.png")
        if n not in times or not os.path.exists(alerts):
            continue
        t = times[n]
        frame = Image.new("RGB", (W, H), BG)
        dash = Image.open(g).convert("RGB").resize((W, 750))
        frame.paste(dash, (0, 70))
        inset = Image.open(alerts).convert("RGB").crop((230, 300, 1370, 480)).resize((520, 82))
        frame.paste(inset, (W - 536, 700))
        d = ImageDraw.Draw(frame)
        faults = [f for f in run["faults"] if f["t"] <= t]
        caption = f"latest fault: {faults[-1]['fault']} (t={faults[-1]['t']:.0f}s)" if faults else "steady load, no faults yet"
        d.text((24, 14), f"Chaos run  t = {max(t, 0):3.0f} s", fill=ACCENT, font=big)
        d.text((24, 44), caption, fill=INK, font=small)
        d.text((W - 536, 676), "Alertmanager: alerts grouped by owning team", fill=INK, font=small)
        out.append(frame.quantize(colors=128, method=Image.Quantize.MEDIANCUT))
    path = os.path.join(os.path.dirname(__file__), "..", "docs", "img", "demo.gif")
    out[0].save(path, save_all=True, append_images=out[1:], duration=1100, loop=0, optimize=True)
    print(path, len(out), "frames", os.path.getsize(path) // 1024, "KB")


if __name__ == "__main__":
    main(*sys.argv[1:4])
