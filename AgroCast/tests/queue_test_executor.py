import json
import os
import subprocess
import time
from pathlib import Path


def _pid_marker(args):
    marker = args.get("pid_file")
    if marker:
        Path(marker).write_text(json.dumps({"pid": os.getpid(), "pgid": os.getpgid(0)}), encoding="utf-8")


def sleepy(params, log):
    _pid_marker(params)
    seconds = float(params.get("seconds") or 0.0)
    log("sleepy: start")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)
    log("sleepy: done")
    return {
        "slept": seconds,
        "environment": {
            "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS"),
            "OPENBLAS_NUM_THREADS": os.environ.get("OPENBLAS_NUM_THREADS"),
            "MKL_NUM_THREADS": os.environ.get("MKL_NUM_THREADS"),
            "agrocast_variables": sorted(
                name for name in os.environ if name.startswith("AGROCAST_") and name != "AGROCAST_LOG_LEVEL"
            ),
        },
    }


def grandchild(params, log):
    _pid_marker(params)
    log("grandchild: spawning detached child")
    child = subprocess.Popen(["sleep", str(params.get("child_seconds", 40))])
    Path(params["child_pid_file"]).write_text(str(child.pid), encoding="utf-8")
    time.sleep(float(params.get("seconds") or 60.0))
    return {"finished_despite_cancel": True}


def probe(params, log):
    import resource

    status = {}
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition(":")
        if key in {"NoNewPrivs", "Seccomp", "CapEff"}:
            status[key] = value.strip()
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    trace_return = libc.ptrace(0, 0, None, None)
    trace_errno = ctypes.get_errno()
    return {
        "process": status,
        "ptrace_blocked": bool(trace_return == -1 and trace_errno in {1, 38}),
        "ptrace_errno": trace_errno,
        "rlimit_cpu": list(resource.getrlimit(resource.RLIMIT_CPU)),
        "rlimit_nofile": list(resource.getrlimit(resource.RLIMIT_NOFILE)),
        "cwd": os.getcwd(),
        "omp": os.environ.get("OMP_NUM_THREADS"),
        "openblas": os.environ.get("OPENBLAS_NUM_THREADS"),
    }


def boom(params, log):
    raise RuntimeError("boom from the injected executor")


HANDLERS = {"sleepy": sleepy, "grandchild": grandchild, "probe": probe, "boom": boom}
