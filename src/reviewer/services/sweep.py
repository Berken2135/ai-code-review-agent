"""Startup sweep: fail runs that a previous process left queued or running.

Reviews run as in-process background tasks, so a restart loses them. Without this sweep they would
stay 'running' forever. ponytail: assumes a single app process; with several instances a restart
of one would also fail the others' in-flight runs (a heartbeat or a real queue would fix that).
"""

import structlog

from reviewer.db.repo import RunRepository
from reviewer.db.session import session_scope

log = structlog.get_logger(__name__)

REASON = (
    "The app restarted while this review was queued or running; "
    "in-process background tasks do not survive a restart."
)


def sweep_stuck_runs() -> int:
    """Returns how many runs were failed. Never raises: an unreachable DB must not stop startup."""
    try:
        with session_scope() as session:
            count = RunRepository(session).fail_stuck_runs(REASON)
    except Exception as exc:
        log.warning("startup_sweep_failed", error=type(exc).__name__, detail=str(exc)[:200])
        return 0
    log.info("startup_sweep_done", failed_runs=count)
    return count
