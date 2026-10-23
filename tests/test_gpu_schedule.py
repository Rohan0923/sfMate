import threading
from datetime import datetime

import pytest

from app import config, gpu_schedule

TZ = gpu_schedule.SCHOOL_TZ


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)   # Oct 22 is a Thursday, Oct 24 a Saturday


@pytest.fixture(autouse=True)
def warm_7_to_22(monkeypatch):
    monkeypatch.setattr(config, "GPU_WARM_HOURS", (7, 22))
    monkeypatch.setattr(config, "GPU_WARM_WEEKDAYS_ONLY", False)
    monkeypatch.setattr(config, "GPU_WARM_LEAD_MINUTES", 10)
    monkeypatch.setattr(config, "RUNPOD_API_KEY", "rp_test_key")
    monkeypatch.setattr(config, "RUNPOD_ENDPOINT_ID", "ep123")


@pytest.mark.parametrize("when,expected", [
    (at(22, 6, 49), 0), (at(22, 6, 50), 1),    # starts 10 minutes early so the model is loaded by 7:00
    (at(22, 12), 1), (at(22, 21, 59), 1),
    (at(22, 22, 0), 0), (at(22, 23, 30), 0), (at(22, 3), 0),
    (at(24, 12), 1),                           # Saturday: every day unless weekdays-only
])
def test_wanted_workers_follows_the_clock(when, expected):
    assert gpu_schedule.wanted_workers(when) == expected


def test_weekdays_only_skips_the_weekend(monkeypatch):
    monkeypatch.setattr(config, "GPU_WARM_WEEKDAYS_ONLY", True)
    assert gpu_schedule.wanted_workers(at(24, 12)) == 0 and gpu_schedule.wanted_workers(at(22, 12)) == 1


def test_blank_hours_leave_the_gpu_alone(monkeypatch):
    monkeypatch.setattr(config, "GPU_WARM_HOURS", ())
    assert gpu_schedule.wanted_workers(at(22, 12)) == 0
    assert gpu_schedule.start() is False and not gpu_schedule.enabled()


class FakeRunPod:
    def __init__(self, workers_min=0):
        self.workers_min, self.calls = workers_min, []

    def request(self, method, url, headers=None, timeout=None, json=None):
        self.calls.append((method, url, json, headers["Authorization"]))
        if method == "PATCH":
            self.workers_min = json["workersMin"]
        outer = self

        class Response:
            def raise_for_status(self): pass
            def json(self): return {"workersMin": outer.workers_min, "gpuTypeIds": ["x"]}
        return Response()


@pytest.fixture
def runpod(monkeypatch):
    fake = FakeRunPod()
    monkeypatch.setattr(gpu_schedule.requests, "request", fake.request)
    return fake


def test_it_turns_the_gpu_on_for_the_hours_and_back_off_after(runpod):
    assert gpu_schedule.sync(at(22, 12)) == 1
    assert runpod.calls[-1] == ("PATCH", "https://rest.runpod.io/v1/endpoints/ep123", {"workersMin": 1}, "Bearer rp_test_key")
    assert gpu_schedule.sync(at(22, 13)) is None and len(runpod.calls) == 3   # already 1: checked, not changed
    assert gpu_schedule.sync(at(22, 22, 5)) == 0 and runpod.workers_min == 0


def test_it_only_ever_sends_the_minimum(runpod):
    gpu_schedule.sync(at(22, 12))
    patches = [json for method, _, json, _ in runpod.calls if method == "PATCH"]
    assert patches == [{"workersMin": 1}]   # never the model, GPUs, or anything else


def test_a_failing_check_never_kills_the_scheduler(monkeypatch):
    def boom(*a, **k): raise RuntimeError("RunPod is down")
    monkeypatch.setattr(gpu_schedule.requests, "request", boom)
    stop = threading.Event()
    monkeypatch.setattr(stop, "wait", lambda seconds: stop.set() or True)   # one pass, then stop
    gpu_schedule._scheduler(stop)   # returns instead of raising
