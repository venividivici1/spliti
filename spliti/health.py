"""Lightweight system-health sampling for the /health dashboard.

Reads CPU / memory / disk / network counters via psutil. To stay cheap when
several dashboards stream at once, a fresh sample is computed at most once per
``_MIN_INTERVAL`` seconds and cached behind a lock — so N concurrent viewers
cost roughly the same as one. Throughput figures (network, disk IO) are derived
from the delta between consecutive samples.
"""

import socket
import threading
import time

import psutil

# Don't recompute a fresh sample more often than this, however many viewers are
# connected. The SSE stream ticks slower than this anyway (see app.py).
_MIN_INTERVAL = 1.0

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "snapshot": None}        # monotonic ts + last snapshot
_prev: dict = {"ts": None, "net": None, "disk": None}  # for rate deltas

# Prime cpu_percent so the first real sample reflects usage since import (a first
# interval=None call always returns 0.0 — it has nothing to diff against yet).
psutil.cpu_percent(interval=None)


def _rate(curr: float, prev: float | None, dt: float) -> float:
    """Per-second rate from a monotonic counter delta; 0 on the first sample."""
    if prev is None or dt <= 0:
        return 0.0
    return max(0.0, (curr - prev) / dt)


def _compute(now: float) -> dict:
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    du = psutil.disk_usage("/")
    net = psutil.net_io_counters()
    dio = psutil.disk_io_counters()

    dt = (now - _prev["ts"]) if _prev["ts"] is not None else 0.0
    prev_net, prev_disk = _prev["net"], _prev["disk"]
    sent_bps = _rate(net.bytes_sent, prev_net.bytes_sent if prev_net else None, dt)
    recv_bps = _rate(net.bytes_recv, prev_net.bytes_recv if prev_net else None, dt)
    read_bps = _rate(dio.read_bytes, prev_disk.read_bytes if (dio and prev_disk) else None, dt) if dio else 0.0
    write_bps = _rate(dio.write_bytes, prev_disk.write_bytes if (dio and prev_disk) else None, dt) if dio else 0.0

    _prev.update(ts=now, net=net, disk=dio)

    try:
        load = list(psutil.getloadavg())
    except (AttributeError, OSError):  # not available on some platforms
        load = None

    return {
        "ts": time.time(),
        "uptime_sec": int(time.time() - psutil.boot_time()),
        "host": socket.gethostname(),
        "cpu": {
            "percent": psutil.cpu_percent(interval=None),
            "per_core": psutil.cpu_percent(interval=None, percpu=True),
            "count": psutil.cpu_count(logical=True),
            "load_avg": load,
        },
        "memory": {
            "total": vm.total, "used": vm.used,
            "available": vm.available, "percent": vm.percent,
        },
        "swap": {"total": sm.total, "used": sm.used, "percent": sm.percent},
        "disk": {
            "total": du.total, "used": du.used, "free": du.free, "percent": du.percent,
            "read_bps": read_bps, "write_bps": write_bps,
        },
        "network": {
            "sent_bps": sent_bps, "recv_bps": recv_bps,
            "bytes_sent": net.bytes_sent, "bytes_recv": net.bytes_recv,
        },
    }


def sample() -> dict:
    """Return a current metrics snapshot, reusing a cached one within _MIN_INTERVAL."""
    now = time.monotonic()
    with _lock:
        snap = _cache["snapshot"]
        if snap is not None and now - _cache["ts"] < _MIN_INTERVAL:
            return snap
        snap = _compute(now)
        _cache.update(ts=now, snapshot=snap)
        return snap
