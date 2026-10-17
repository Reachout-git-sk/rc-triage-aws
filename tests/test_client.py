import pytest
import requests
import responses

from triage.client import IngestClient, IngestError

URL = "http://api.test"
INGEST = f"{URL}/api/ingest"


def make_client(sleeps: list, attempts: int = 4) -> IngestClient:
    return IngestClient(URL, "key", max_attempts=attempts, sleep=sleeps.append)


@responses.activate
def test_retries_server_errors_then_succeeds():
    responses.post(INGEST, status=503)
    responses.post(INGEST, status=502)
    responses.post(INGEST, json={"inserted": 3})
    sleeps: list = []
    assert make_client(sleeps).post({"run_id": "r1", "failures": []}) == {"inserted": 3}
    assert len(responses.calls) == 3
    assert len(sleeps) == 2
    assert 0 <= sleeps[0] <= 0.5 and 0 <= sleeps[1] <= 1.0  # full jitter under a doubling cap
    assert responses.calls[0].request.headers["X-API-Key"] == "key"


@responses.activate
def test_does_not_retry_client_errors():
    responses.post(INGEST, status=400, json={"error": "invalid payload"})
    sleeps: list = []
    with pytest.raises(IngestError) as err:
        make_client(sleeps).post({"run_id": "r1"})
    assert err.value.status == 400 and not err.value.retryable
    assert len(responses.calls) == 1 and sleeps == []


@responses.activate
def test_gives_up_after_max_attempts_on_timeouts():
    responses.post(INGEST, body=requests.exceptions.ConnectTimeout())
    sleeps: list = []
    with pytest.raises(IngestError) as err:
        make_client(sleeps, attempts=3).post({"run_id": "r1"})
    assert err.value.retryable
    assert len(responses.calls) == 3 and len(sleeps) == 2


@responses.activate
def test_honors_retry_after():
    responses.post(INGEST, status=429, headers={"Retry-After": "7"})
    responses.post(INGEST, json={})
    sleeps: list = []
    make_client(sleeps).post({"run_id": "r1"})
    assert sleeps == [7.0]
