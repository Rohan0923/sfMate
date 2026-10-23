"""Keeps one GPU worker running, with the model already loaded, during set hours, so nobody waits through a cold start.

RunPod's "min workers" setting does it: 1 keeps a worker up all the time (billed even while idle), 0 lets the endpoint
sleep when nobody's asking. This thread sets it from the clock (school time), starting GPU_WARM_LEAD_MINUTES early so
the model is loaded by the time the hours begin. Outside the hours the worker stops after the endpoint's idle timeout.
Only the minimum changes; the model, GPUs, and everything else on the endpoint are left alone."""
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from . import config

log = logging.getLogger("sfmate.gpu")

SCHOOL_TZ = ZoneInfo(config.SCHOOL_TIMEZONE)
CHECK_SECONDS = 120
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; sfMate/1.0)"}   # RunPod's firewall turns away unnamed scripts

_stop = threading.Event()


def enabled():
    return bool(config.GPU_WARM_HOURS and config.RUNPOD_API_KEY and config.RUNPOD_ENDPOINT_ID)


def wanted_workers(now):
    """1 inside the warm hours (plus the lead time before them), 0 otherwise."""
    if not config.GPU_WARM_HOURS or (config.GPU_WARM_WEEKDAYS_ONLY and now.weekday() >= 5):
        return 0
    start, end = config.GPU_WARM_HOURS
    minute = now.hour * 60 + now.minute
    return int(start * 60 - config.GPU_WARM_LEAD_MINUTES <= minute < end * 60)


def _url():
    return f"https://rest.runpod.io/v1/endpoints/{config.RUNPOD_ENDPOINT_ID}"


def _call(method, **kwargs):
    r = requests.request(method, _url(), headers={**HEADERS, "Authorization": f"Bearer {config.RUNPOD_API_KEY}"},
                         timeout=20, **kwargs)
    r.raise_for_status()
    return r.json()


def sync(now=None):
    """Makes RunPod's minimum worker count match the clock. Returns the new count if it changed it, else None."""
    want = wanted_workers(now or datetime.now(SCHOOL_TZ))
    have = _call("GET").get("workersMin")
    if have == want:
        return None
    _call("PATCH", json={"workersMin": want})   # only this field: the rest of the endpoint stays as it is
    log.info("GPU warm hours: minimum workers %s -> %s", have, want)
    return want


def _scheduler(stop):
    last_error = None
    while True:
        try:
            sync()
            last_error = None
        except Exception as e:
            message = f"{type(e).__name__}: {e}"
            if message != last_error:   # a key that can't change endpoints would otherwise repeat this every check
                log.error("couldn't set the GPU's minimum workers (will keep trying): %s", message)
            last_error = message
        if stop.wait(CHECK_SECONDS):
            return


def start():
    if not enabled():
        log.info("the GPU isn't kept warm: set GPU_WARM_HOURS (like 7-22) to keep it loaded during those hours")
        return False
    _stop.clear()
    threading.Thread(target=_scheduler, args=(_stop,), daemon=True, name="gpu-schedule").start()
    start_hour, end_hour = config.GPU_WARM_HOURS
    log.info("GPU kept warm %d:00-%d:00 school time%s, from %d minutes early", start_hour, end_hour,
             " on weekdays" if config.GPU_WARM_WEEKDAYS_ONLY else "", config.GPU_WARM_LEAD_MINUTES)
    return True


def stop():
    _stop.set()   # leaves RunPod as it is: the worker winds down by its own idle timeout
