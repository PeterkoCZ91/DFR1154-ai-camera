"""Whether a face verdict may open the door, and the call that opens it.

Kept apart from pipeline.py so the decision is a pure function that tests can
hammer without a camera, and so the Home Assistant call can be checked against
what HA actually answered rather than against what we hoped it would.

Everything here fails closed: a missing score, an unlisted name, a cooldown in
progress or a switch that is off all answer "no".
"""

import time
from typing import Callable, Iterable, Optional, Tuple

import requests

from .face_result import FaceResult

# Lock states that mean the door is, or is about to be, open. Nuki reports
# `unlocking` while the motor runs and `open`/`unlatched` once the latch is
# pulled; anything else after the service call means it did not work.
_OPENED_STATES = frozenset({"unlocked", "unlocking", "unlatched", "open", "opening"})


def unlock_decision(
    face: FaceResult,
    *,
    enabled: bool,
    allowed_names: Iterable[str],
    min_score: float,
    now: float,
    last_unlock_at: float,
    cooldown: float,
) -> Tuple[bool, str]:
    """Answer (allowed, reason). The reason is what goes into the log."""
    if not enabled:
        return False, "disabled"
    if not face.is_resident or not face.name:
        return False, f"not_resident:{face.outcome.value}"
    # The verdict's own name only says a resident is somewhere in view. The
    # door goes by the largest face, the person actually standing at it, so a
    # resident behind a stranger does not open it for the stranger.
    if not face.lead_name:
        return False, "lead_face_not_resident"
    if face.lead_name not in set(allowed_names):
        return False, "name_not_allowed"
    # No score means the matcher never said how close it was; opening a door on
    # an unmeasured match is exactly what this gate exists to prevent.
    if face.lead_score is None:
        return False, "no_score"
    if face.lead_score < min_score:
        return False, f"score_below_min:{face.lead_score:.3f}<{min_score:.3f}"
    if now - last_unlock_at < cooldown:
        return False, "cooldown"
    return True, "ok"


def ha_unlock(
    ha_url: str,
    token: str,
    entity_id: str,
    *,
    post: Callable = requests.post,
    get: Callable = requests.get,
    sleep: Callable[[float], None] = time.sleep,
    settle_seconds: float = 6.0,
    timeout: float = 5.0,
) -> Tuple[bool, str]:
    """Call lock.unlock and confirm the lock moved. Returns (opened, detail).

    A 2xx from the service call only says HA accepted the request. An expired
    token is a 401 and a dead Nuki bridge still answers 200, so the state is
    read back before anything is reported as unlocked.
    """
    if not ha_url or not token:
        return False, "no_ha_config"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = ha_url.rstrip("/")
    try:
        resp = post(
            f"{base}/api/services/lock/unlock",
            headers=headers,
            json={"entity_id": entity_id},
            timeout=timeout,
        )
        resp.raise_for_status()
    except Exception as e:
        return False, f"service_call_failed:{e}"

    state: Optional[str] = None
    waited = 0.0
    while True:
        try:
            r = get(f"{base}/api/states/{entity_id}", headers=headers, timeout=timeout)
            r.raise_for_status()
            state = r.json().get("state")
        except Exception as e:
            state = f"unreadable:{e}"
        if state in _OPENED_STATES:
            return True, str(state)
        if waited >= settle_seconds:
            return False, f"state_after_unlock:{state}"
        sleep(1.0)
        waited += 1.0
