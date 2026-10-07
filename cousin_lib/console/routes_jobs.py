"""Jobs routes (docs/reference/console-api.md, "Jobs"): a projection of
cousin_lib.jobs with the rate-limited reaper as maintenance the store
accepts from any reader, log tails, field updates with SIGTERM on
cancel, and delete limited to console-minted logs."""
from __future__ import annotations

import os
import signal
import sys
import time

from cousin_lib import jobs
from cousin_lib.console import router
from cousin_lib.console.app import HttpError

REAP_INTERVAL_SECONDS = 300
TAIL_BYTES = 64 * 1024


def _maintenance(server):
    now = time.time()
    if now - server.state.get("jobs_reap_ts", 0) < REAP_INTERVAL_SECONDS:
        return
    server.state["jobs_reap_ts"] = now
    try:
        jobs.reap_stale(max_age_hours=24)
        jobs.rotate(cap=1000)
    except Exception as err:  # noqa: BLE001 - never a 500 on the poll
        print("[console] jobs maintenance failed: %s" % err,
              file=sys.stderr, flush=True)


def _reap_lost():
    """A running row whose process is gone turns 'lost' on every read
    (cheap: one /proc look per running row), not every 5 minutes."""
    try:
        jobs.reap_lost()
    except Exception as err:  # noqa: BLE001 - never a 500 on the poll
        print("[console] lost-job check failed: %s" % err,
              file=sys.stderr, flush=True)


def _job_id(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HttpError(404, "unknown job")


def _job(raw):
    job = jobs.get_job(_job_id(raw))
    if job is None:
        raise HttpError(404, "unknown job")
    return job


def _tail(path, *, lines, from_byte):
    """(text, size, next): the tail or the bytes from an offset, and the
    offset a follower asks for next. A read that hit the TAIL_BYTES cap
    ends on the last newline inside it, so a line (and a multi-byte
    character) is never split across two reads."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return "(log not written yet: %s)" % path, 0, 0
    with open(path, "rb") as fh:
        if from_byte is not None:
            start = max(0, min(from_byte, size))
            fh.seek(start)
            chunk = fh.read(TAIL_BYTES)
            if len(chunk) == TAIL_BYTES and start + len(chunk) < size:
                cut = chunk.rfind(b"\n")
                if cut >= 0:
                    chunk = chunk[:cut + 1]
            return (chunk.decode("utf-8", "replace"), size,
                    start + len(chunk))
        fh.seek(max(0, size - TAIL_BYTES))
        text = fh.read().decode("utf-8", "replace")
    parts = text.splitlines(keepends=True)
    return "".join(parts[-lines:]), size, size


def register():
    @router.route("GET", "/api/jobs")
    def list_jobs(req):
        _maintenance(req.server)
        _reap_lost()
        q = req.query
        rows = jobs.list_jobs(
            status=q.get("status") or None,
            spawned_by=q.get("spawned_by") or None,
            kind=q.get("kind") or None,
            active_only=q.get("active_only") in ("1", "true"),
            since_hours=req.int_query("since_hours"),
            limit=req.int_query("limit", 200),
            running_first=True)
        return 200, {"jobs": rows}

    @router.route("GET", "/api/jobs/{job_id}")
    def show(req, job_id):
        _reap_lost()
        return 200, {"ok": True, "job": _job(job_id)}

    @router.route("GET", "/api/jobs/{job_id}/log")
    def log(req, job_id):
        job = _job(job_id)
        lines = req.int_query("lines", 40)
        from_byte = req.int_query("from")
        if not job.get("log_path"):
            return 200, {"ok": True, "log": "", "log_path": None, "size": 0,
                         "next": 0, "has_log": False}
        text, size, nxt = _tail(job["log_path"], lines=max(1, lines),
                                from_byte=from_byte)
        return 200, {"ok": True, "log": text, "log_path": job["log_path"],
                     "size": size, "next": nxt, "has_log": True}

    @router.route("POST", "/api/jobs/{job_id}")
    def update(req, job_id):
        job = _job(job_id)
        body = req.body
        fields = {k: body[k] for k in jobs._UPDATABLE
                  if k in body and body[k] is not None}
        if not fields:
            raise HttpError(400, "no updatable field given")
        status = fields.get("status")
        if status is not None and status not in jobs.STATUSES:
            raise HttpError(400, "status must be one of %s"
                            % "|".join(jobs.STATUSES))
        if status == "cancelled" and job["status"] == "running" \
                and job.get("pid") and not job.get("pgid"):
            # A row from before process groups were recorded.
            try:
                os.kill(int(job["pid"]), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, ValueError):
                pass
        try:
            row = jobs.update_job(job["id"], **fields)
        except ValueError as err:
            raise HttpError(400, str(err))
        if row is None:
            raise HttpError(404, "unknown job")
        if status in ("cancelled", "done", "failed"):
            # Closing a job ends its processes, the same as cousin-job.
            jobs.reap_group(job)
        req.server.emit("job-update", row)
        return 200, {"ok": True, "job": row}

    @router.route("DELETE", "/api/jobs/{job_id}")
    def delete(req, job_id):
        job = _job(job_id)
        if jobs.is_minted_log(job.get("log_path")):
            try:
                os.unlink(job["log_path"])
            except OSError:
                pass
        jobs.delete_job(job["id"])
        req.server.emit("job-delete", {"id": job["id"]})
        return 200, {"ok": True}


register()
