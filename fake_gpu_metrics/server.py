"""A fake GPU-metrics service: the "upstream" the API calls for node health.

It exists to practise failure handling. Switch its behaviour at runtime:
  normal  answer at once
  slow    sleep FAKE_GPU_SLOW_SECONDS (default 5) before answering
  down    always 503
  flaky   503 half the time

  curl -X POST localhost:9100/admin/mode -H 'Content-Type: application/json' -d '{"mode": "slow"}'

All numbers are made up; two nodes match the incidents planted by synth/generate.py.
"""

from __future__ import annotations

import os
import random
import time

from flask import Flask, request

MODES = ("normal", "slow", "down", "flaky")
XID_EVENTS = {"gpu-013": [79], "gpu-027": [48, 63]}

app = Flask(__name__)
_state = {"mode": os.environ.get("FAKE_GPU_MODE", "normal")}


@app.get("/health")
def health():
    return {"status": "ok", "mode": _state["mode"]}


@app.route("/admin/mode", methods=["GET", "POST"])
def mode():
    if request.method == "POST":
        wanted = (request.get_json(silent=True) or {}).get("mode")
        if wanted not in MODES:
            return {"error": f"mode must be one of {list(MODES)}"}, 400
        _state["mode"] = wanted
    return {"mode": _state["mode"]}


@app.get("/metrics/<node>")
def metrics(node: str):
    if not node.startswith("gpu-"):
        return {"error": f"{node} has no GPUs"}, 404
    current = _state["mode"]
    if current == "down" or (current == "flaky" and random.random() < 0.5):
        return {"error": "metrics backend unavailable"}, 503
    if current == "slow":
        time.sleep(float(os.environ.get("FAKE_GPU_SLOW_SECONDS", "5")))
    rng = random.Random(node)  # same node -> same numbers
    xids = XID_EVENTS.get(node, [])
    return {
        "node": node,
        "synthetic": True,
        "gpus": [
            {
                "index": i,
                "temperature_c": rng.randint(34, 81),
                "utilization_pct": rng.randint(0, 100),
                "memory_used_mb": rng.randint(0, 40_000),
                "memory_total_mb": 40_960,
                "ecc_double_bit_errors": 1 if (48 in xids and i == 0) else 0,
            }
            for i in range(4)
        ],
        "xid_events_24h": xids,
    }
