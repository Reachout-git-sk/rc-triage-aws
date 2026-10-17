import requests
import responses

from api.upstream import GpuMetricsClient

BASE = "http://gpu.test"


def client(**kw) -> GpuMetricsClient:
    kw.setdefault("sleep", lambda s: None)
    return GpuMetricsClient(BASE, retries=2, budget_s=3.0, **kw)


@responses.activate
def test_success_returns_data():
    responses.get(f"{BASE}/metrics/gpu-001", json={"node": "gpu-001", "gpus": []})
    r = client().node_metrics("gpu-001")
    assert r.data == {"node": "gpu-001", "gpus": []} and r.warning is None and r.attempts == 1


@responses.activate
def test_not_a_gpu_node_is_not_a_warning():
    responses.get(f"{BASE}/metrics/cpu-001", status=404)
    r = client().node_metrics("cpu-001")
    assert r.data is None and r.warning is None


@responses.activate
def test_one_timeout_then_success():
    responses.get(f"{BASE}/metrics/gpu-001", body=requests.exceptions.ReadTimeout())
    responses.get(f"{BASE}/metrics/gpu-001", json={"ok": True})
    r = client().node_metrics("gpu-001")
    assert r.data == {"ok": True} and r.attempts == 2


@responses.activate
def test_always_down_gives_warning_after_all_attempts():
    responses.get(f"{BASE}/metrics/gpu-001", status=503)
    r = client().node_metrics("gpu-001")
    assert r.data is None
    assert r.warning == "gpu_metrics unavailable: HTTP 503 after 3 attempt(s)"
    assert len(responses.calls) == 3


@responses.activate
def test_time_budget_stops_retries_early():
    responses.get(f"{BASE}/metrics/gpu-001", body=requests.exceptions.ReadTimeout())
    now = [0.0]

    def fake_sleep(seconds):
        now[0] += seconds

    def fake_clock():
        now[0] += 1.6  # every attempt "takes" 1.6 s
        return now[0]

    r = GpuMetricsClient(
        BASE, retries=5, budget_s=3.0, sleep=fake_sleep, clock=fake_clock
    ).node_metrics("gpu-001")
    assert r.data is None and "time budget" in r.warning
    assert len(responses.calls) < 6
    assert r.attempts == len(responses.calls)
    assert r.warning.endswith(f"after {len(responses.calls)} attempt(s)")


def test_unconfigured_upstream():
    r = GpuMetricsClient("").node_metrics("gpu-001")
    assert r.warning == "gpu_metrics: upstream not configured"
