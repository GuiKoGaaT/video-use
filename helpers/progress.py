"""Progress of a video job, for the Claude Code side pane (mod `video-progress`).

Two files in ~/.claude/video-use/ (override with VIDEO_USE_PROGRESS_DIR):

  progress.json  the job running now: its stages, each with an estimate in
                 seconds, a status and, when the script knows it, a real fraction.
  timings.json   history: seconds of processing per unit, per stage key, learned
                 from every stage that finishes. Estimates come from here.

A unit is whatever the stage scales with: seconds of source for transcribe and
matte, seconds of output for segments/composite/loudnorm, one slot for an
animation built by a sub-agent. Keys name the stage and the hardware or size
that changes its speed: "matte.rvm.cpu", "composite.1920", "anim.hyperframes".

Python use (each helper is its own process; the file is shared and locked):

    import progress
    progress.plan("piloto", [
        {"id": "transcribe", "label": "Transcrição", "key": "transcribe.local.cpu", "units": 60.4},
        {"id": "matte",      "label": "Recorte (matte)", "key": "matte.rvm.cpu", "units": 60.4},
    ])
    with progress.stage("matte", units=60.4, key="matte.rvm.cpu") as st:
        for i in ...: st.update(i / n)

    progress.run_ffmpeg(cmd, stage_id="composite", total_s=13.8)  # -progress parsed

A `stage()` whose id is not in the plan is appended; with no running job it
starts one named after the working folder. Nothing here ever raises into the
caller: progress is best effort and must not break a render.

CLI (for steps run by hand or by sub-agents):

    progress.py plan --job NAME [--dir D] --stage id:label:key:units [...]
    progress.py begin ID [--label L] [--key K] [--units N]
    progress.py update ID FRACTION [--note TEXT]
    progress.py done ID | fail ID [--note TEXT] | skip ID
    progress.py show | rates
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(os.environ.get("VIDEO_USE_PROGRESS_DIR", Path.home() / ".claude" / "video-use"))
PROGRESS = DIR / "progress.json"
TIMINGS = DIR / "timings.json"
LOCK = DIR / "progress.lock"
KEEP_SAMPLES = 12

# Seconds of processing per unit, used until a key has history of its own.
# Measured on the first session (Windows, CPU only, 464x832 60 fps source);
# 1920 render rates scaled from the measured 960 ones.
DEFAULT_RATES = {
    "transcribe.local.cpu": 1.83,     # WhisperX medium, s per s of source
    "transcribe.local.cuda": 0.25,
    "transcribe.scribe": 0.35,
    "matte.rvm.cpu": 6.53,            # RVM mobilenetv3, s per s of source
    "matte.rvm.cuda": 0.8,
    "matte.chroma": 0.4,
    "anim.hyperframes": 385.0,        # one slot built + rendered by a sub-agent
    "anim.remotion": 420.0,
    "anim.pil": 60.0,
    "segments.960": 0.35,              # s per s of output
    "segments.1920": 1.3,
    "composite.960": 0.2,
    "composite.1920": 0.9,
    "loudnorm": 0.06,
}
FALLBACK_RATE = 1.0


# ---------------------------------------------------------------- file io --

@contextlib.contextmanager
def _locked(timeout: float = 5.0):
    DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    fd = None
    while fd is None:
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - LOCK.stat().st_mtime > 10:   # stale lock
                    LOCK.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.time() - t0 > timeout:
                break                                         # proceed unlocked
            time.sleep(0.02)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            with contextlib.suppress(OSError):
                LOCK.unlink()


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    for _ in range(20):                       # Windows: the pane may be reading
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.03)
    tmp.unlink(missing_ok=True)


def _safe(fn):
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as exc:  # progress must never break the job
            print(f"  (progress: {exc.__class__.__name__}: {exc})", file=sys.stderr)
            return None
    return wrapper


# ------------------------------------------------------------- estimates --

def rate(key: str | None) -> float:
    if key:
        hist = _read(TIMINGS, {}).get(key)
        if hist and hist.get("samples"):
            s, n = sorted(hist["samples"]), len(hist["samples"])
            return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2   # median
        if key in DEFAULT_RATES:
            return DEFAULT_RATES[key]
        base = key.split(".")[0]
        for k, v in DEFAULT_RATES.items():
            if k.split(".")[0] == base:
                return v
    return FALLBACK_RATE


def estimate(key: str | None, units: float | None) -> float:
    return round(rate(key) * (units if units else 1.0), 1)


def _record(key: str | None, units: float | None, elapsed: float) -> None:
    if not key or elapsed <= 0:
        return
    u = units if units else 1.0
    with _locked():
        t = _read(TIMINGS, {})
        h = t.setdefault(key, {"samples": []})
        h["samples"] = (h["samples"] + [round(elapsed / u, 4)])[-KEEP_SAMPLES:]
        h["last"] = {"units": u, "elapsed_s": round(elapsed, 1), "at": time.time()}
        _write(TIMINGS, t)


# ------------------------------------------------------------------ job --

def _new_stage(sid, label=None, key=None, units=None):
    return {"id": sid, "label": label or sid, "key": key, "units": units,
            "estimate_s": estimate(key, units), "status": "pending",
            "started_at": None, "done_at": None, "fraction": None, "note": None}


def _job_running(job) -> bool:
    return bool(job) and job.get("status") == "running"


@_safe
def plan(job: str, stages: list[dict], edit_dir: str | Path | None = None) -> None:
    """Start a job: every stage it will go through, so the overall bar is honest."""
    now = time.time()
    data = {"job": job, "dir": str(edit_dir or Path.cwd()), "started_at": now,
            "updated_at": now, "status": "running", "finished_at": None,
            "stages": [_new_stage(s["id"], s.get("label"), s.get("key"), s.get("units"))
                       for s in stages]}
    with _locked():
        _write(PROGRESS, data)


@_safe
def ensure(job: str, stages: list[dict], edit_dir: str | Path | None = None) -> None:
    """Add these stages to the running job (refreshing their key/units/estimate),
    or plan a new job with them when none runs. Lets a render script join the
    plan the agent made for the whole edit instead of replacing it."""
    with _locked():
        cur = _read(PROGRESS, None)
        if not _job_running(cur):
            cur = None
        if cur is None:
            now = time.time()
            cur = {"job": job, "dir": str(edit_dir or Path.cwd()), "started_at": now,
                   "updated_at": now, "status": "running", "finished_at": None, "stages": []}
        for spec in stages:
            st = next((x for x in cur["stages"] if x["id"] == spec["id"]), None)
            if st is None:
                cur["stages"].append(_new_stage(spec["id"], spec.get("label"), spec.get("key"), spec.get("units")))
            elif st["status"] in ("pending", "done", "skipped", "failed"):
                st.update(label=spec.get("label") or st["label"], key=spec.get("key") or st["key"],
                          units=spec.get("units") or st["units"], status="pending",
                          started_at=None, done_at=None, fraction=None, note=None)
                st["estimate_s"] = estimate(st["key"], st["units"])
        cur["updated_at"] = time.time()
        _write(PROGRESS, cur)


def _mutate(sid: str, fn, create: dict | None = None) -> None:
    with _locked():
        job = _read(PROGRESS, None)
        retry = bool(job) and job.get("status") == "failed" and any(s["id"] == sid for s in job["stages"])
        if not (_job_running(job) or retry):
            now = time.time()
            job = {"job": Path.cwd().name, "dir": str(Path.cwd()), "started_at": now,
                   "updated_at": now, "status": "running", "finished_at": None, "stages": []}
        st = next((s for s in job["stages"] if s["id"] == sid), None)
        if st is None:
            st = _new_stage(sid, **(create or {}))
            job["stages"].append(st)
        fn(st)
        job["updated_at"] = time.time()
        if any(s["status"] == "failed" for s in job["stages"]):
            job["status"], job["finished_at"] = "failed", job["updated_at"]
        elif all(s["status"] in ("done", "skipped") for s in job["stages"]):
            job["status"], job["finished_at"] = "done", job["updated_at"]
        else:
            job["status"], job["finished_at"] = "running", None
        _write(PROGRESS, job)


@_safe
def begin(sid: str, label=None, key=None, units=None) -> None:
    def go(st):
        if label: st["label"] = label
        if key: st["key"] = key
        if units: st["units"] = units
        if key or units: st["estimate_s"] = estimate(st["key"], st["units"])
        st.update(status="running", started_at=time.time(), done_at=None, fraction=None, note=None)
    _mutate(sid, go, {"label": label, "key": key, "units": units})


@_safe
def update(sid: str, fraction: float | None = None, note: str | None = None) -> None:
    def go(st):
        if fraction is not None:
            st["fraction"] = round(max(0.0, min(1.0, fraction)), 4)
        if note is not None:
            st["note"] = note
    _mutate(sid, go)


@_safe
def finish(sid: str, status: str = "done", note: str | None = None) -> None:
    rec = {}
    def go(st):
        now = time.time()
        if status == "done" and st.get("started_at"):
            rec.update(key=st["key"], units=st["units"], elapsed=now - st["started_at"])
        st.update(status=status, done_at=now)
        if status == "done": st["fraction"] = 1.0
        if note is not None: st["note"] = note
    _mutate(sid, go)
    if rec:
        _record(rec["key"], rec["units"], rec["elapsed"])


class _Stage:
    def __init__(self, sid):
        self.sid, self._last = sid, 0.0

    def update(self, fraction: float | None = None, note: str | None = None, force=False):
        now = time.time()
        if force or now - self._last >= 0.5:      # at most 2 writes per second
            self._last = now
            update(self.sid, fraction, note)


@contextlib.contextmanager
def stage(sid: str, label=None, key=None, units=None):
    begin(sid, label, key, units)
    st = _Stage(sid)
    try:
        yield st
    except BaseException as exc:
        finish(sid, "failed", note=f"{exc.__class__.__name__}: {exc}"[:200])
        raise
    else:
        finish(sid, "done")


def run_ffmpeg(cmd: list[str], stage_id: str | None, total_s: float, **kw):
    """Run ffmpeg reporting real progress from -progress; returns CompletedProcess-like."""
    if stage_id is None:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", **kw)
    full = [cmd[0], "-progress", "pipe:1", "-nostats"] + cmd[1:]
    p = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace", **kw)
    st = _Stage(stage_id)
    err_lines: list[str] = []

    import threading
    def drain():
        for line in p.stderr:
            err_lines.append(line)
            del err_lines[:-200]
    th = threading.Thread(target=drain, daemon=True); th.start()
    for line in p.stdout:
        m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
        if m and total_s > 0:
            st.update(int(m.group(1)) / 1e6 / total_s)
    p.wait(); th.join(timeout=2)
    return subprocess.CompletedProcess(full, p.returncode, "", "".join(err_lines))


# ------------------------------------------------------------------- cli --

def _show():
    job = _read(PROGRESS, None)
    if not job:
        print("no job"); return
    print(f"{job['job']}  [{job['status']}]  {job['dir']}")
    for s in job["stages"]:
        frac = s["fraction"]
        print(f"  {s['status']:8} {s['label']:28} est {s['estimate_s']:7.1f}s  "
              f"{'' if frac is None else f'{frac:.0%}'}  {s.get('note') or ''}")


def main():
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Progress for the video-progress pane")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan"); p.add_argument("--job", required=True); p.add_argument("--dir")
    p.add_argument("--stage", action="append", default=[], help="id:label:key:units")
    for name in ("begin",):
        b = sub.add_parser(name); b.add_argument("id"); b.add_argument("--label")
        b.add_argument("--key"); b.add_argument("--units", type=float)
    u = sub.add_parser("update"); u.add_argument("id"); u.add_argument("fraction", type=float)
    u.add_argument("--note")
    for name in ("done", "fail", "skip"):
        f = sub.add_parser(name); f.add_argument("id"); f.add_argument("--note")
    sub.add_parser("show"); sub.add_parser("rates")
    a = ap.parse_args()
    if a.cmd == "plan":
        stages = []
        for spec in a.stage:
            parts = spec.rsplit(":", 3)          # ids may hold a colon (transcribe:piloto)
            if len(parts) < 4:
                parts = (spec.split(":") + ["", "", ""])[:4]
            sid, label, key, units = parts
            stages.append({"id": sid, "label": label or sid, "key": key or None,
                           "units": float(units) if units else None})
        plan(a.job, stages, a.dir)
    elif a.cmd == "begin": begin(a.id, a.label, a.key, a.units)
    elif a.cmd == "update": update(a.id, a.fraction, a.note)
    elif a.cmd == "done": finish(a.id, "done", a.note)
    elif a.cmd == "fail": finish(a.id, "failed", a.note)
    elif a.cmd == "skip": finish(a.id, "skipped", a.note)
    elif a.cmd == "show": _show()
    elif a.cmd == "rates":
        t = _read(TIMINGS, {})
        for k in sorted(set(DEFAULT_RATES) | set(t)):
            n = len(t.get(k, {}).get("samples", []))
            print(f"  {k:24} {rate(k):8.2f} s/unit  ({n} runs{'' if n else ', default'})")


if __name__ == "__main__":
    main()
