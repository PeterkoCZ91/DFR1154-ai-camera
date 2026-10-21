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
# pulled; `unlatching`/`opening` are the transient names for the latter.
_OPENED_STATES = frozenset({"unlocked", "unlocking", "unlatched", "unlatching", "open", "opening"})
# Seen only while lock.open is actually pulling the latch. `unlocked` is
# missing on purpose: it is also the resting state before an unlatch.
_UNLATCH_STATES = frozenset({"unlatched", "unlatching", "open", "opening"})
# States that end the wait at once: the lock reported a fault, or HA lost it.
_JAMMED_STATES = frozenset({"jammed"})
_LOST_STATES = frozenset({"unavailable", "unknown"})


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
    settle_seconds: float = 10.0,
    timeout: float = 5.0,
) -> Tuple[bool, str]:
    """Call lock.open (unlatch) and observe the lock react.

    A 2xx from the service call only says HA accepted the request. An expired
    token is a 401 and a dead Nuki bridge still answers 200, so the state is
    read before and after the call. An already-unlocked, unchanged state is
    not evidence that the latch moved.

    Unlatching a lock that is already `unlocked` often ends back at
    `unlocked` before the first poll, so the state alone cannot confirm it.
    Evidence that counts: the state changed into an open one, a transient
    unlatch state was seen, or HA's `last_changed`/`last_updated` moved past
    what it reported before the call while the state is an open one. An
    unchanged, stale state never counts.
    """
    if not ha_url or not token:
        return False, "no_ha_config"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = ha_url.rstrip("/")
    try:
        before = get(f"{base}/api/states/{entity_id}", headers=headers, timeout=timeout)
        before.raise_for_status()
        initial = before.json()
    except Exception as e:
        return False, f"state_before_open_failed:{e}"
    # A lock HA cannot see may queue the command and obey it later, and its
    # return to "unlocked" would then read as a confirmed opening.
    if initial.get("state") in _LOST_STATES or initial.get("state") in _JAMMED_STATES:
        return False, f"lock_{initial.get('state')}"
    try:
        resp = post(
            f"{base}/api/services/lock/open",
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
            current = r.json()
            state = current.get("state")
            if state in _JAMMED_STATES:
                return False, f"open_failed:{state}"
            if state in _UNLATCH_STATES:
                return True, str(state)
            if state in _OPENED_STATES and (
                state != initial.get("state")
                or _advanced(initial, current, "last_changed")
                or _advanced(initial, current, "last_updated")
            ):
                return True, str(state)
        except Exception as e:
            state = f"unreadable:{e}"
        if waited >= settle_seconds:
            return False, f"open_not_confirmed:{state}"
        sleep(1.0)
        waited += 1.0


def _advanced(initial: dict, current: dict, key: str) -> bool:
    """True when HA reports a newer `key` timestamp than before the call.

    A missing timestamp on either side is not evidence of anything.
    """
    now, then = current.get(key), initial.get(key)
    return now is not None and then is not None and now != then


def ha_lock(
    ha_url: str,
    token: str,
    entity_id: str,
    *,
    post: Callable = requests.post,
    get: Callable = requests.get,
    sleep: Callable[[float], None] = time.sleep,
    settle_seconds: float = 30.0,
    timeout: float = 5.0,
) -> Tuple[bool, str]:
    """Call lock.lock and observe a locked state.

    Locking is slow: the motor turns for several seconds and Nuki Hub reports
    the end over Bluetooth, so `locked` reached HA 12 s after the call once —
    hence the 30 s wait.

    A 2xx from the service call only says HA accepted the request, so the
    state is polled until it reads `locked`. `locking` only says the motor
    started — a bolt that hits the frame turns it into `jammed` — so it is
    waited out, never reported as success. `jammed`, `unavailable` and
    `unknown` end the wait at once as a failure.

    The state read before the call checks that HA is reachable and the token
    valid, and an `unavailable` lock is not sent a command it cannot carry out.
    """
    if not ha_url or not token:
        return False, "no_ha_config"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = ha_url.rstrip("/")
    try:
        before = get(f"{base}/api/states/{entity_id}", headers=headers, timeout=timeout)
        before.raise_for_status()
        initial = before.json()
    except Exception as e:
        return False, f"state_before_lock_failed:{e}"
    if initial.get("state") == "unavailable":
        return False, "lock_failed:unavailable"
    try:
        resp = post(
            f"{base}/api/services/lock/lock",
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
            current = r.json()
            state = current.get("state")
            if state == "locked":
                return True, "locked"
            if state in _JAMMED_STATES or state in _LOST_STATES:
                return False, f"lock_failed:{state}"
        except Exception as e:
            state = f"unreadable:{e}"
        if waited >= settle_seconds:
            return False, f"lock_not_confirmed:{state}"
        sleep(1.0)
        waited += 1.0
