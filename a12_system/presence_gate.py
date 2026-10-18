"""Optional second factor for face unlock: a Home Assistant presence state.

A face match alone can be fooled by a photo or a screen (see face_liveness.py
for how little the landmark check can do about that). When configured, the
door only opens while an HA entity — typically the resident's phone tracker
`person.x` or `device_tracker.x` — reads one of the allowed states.

Fails closed: no entity state, an HTTP error, a timeout or an unexpected state
all answer "no". The answer is cached for a few seconds so one visit makes
one call, and a failure is cached too so a dead HA cannot stall the
detection loop on every confirmation.
"""

import time
from typing import Callable, Iterable, Optional, Tuple

import requests


class PresenceGate:
    def __init__(
        self,
        ha_url: str,
        token: str,
        entity_id: str,
        allowed_states: Iterable[str] = ("home",),
        *,
        get: Callable = requests.get,
        clock: Callable[[], float] = time.monotonic,
        cache_seconds: float = 8.0,
        timeout: float = 2.0,
    ):
        self.ha_url = (ha_url or "").rstrip("/")
        self.token = token or ""
        self.entity_id = (entity_id or "").strip()
        self.allowed_states = frozenset(
            str(s).strip().lower() for s in allowed_states if str(s).strip()
        )
        self._get = get
        self._clock = clock
        self.cache_seconds = max(0.0, float(cache_seconds))
        self.timeout = float(timeout)
        self._cached: Optional[Tuple[float, Tuple[bool, str]]] = None

    def allows(self) -> Tuple[bool, str]:
        """(allowed, reason). The reason goes into the log."""
        now = self._clock()
        if self._cached is not None and now - self._cached[0] < self.cache_seconds:
            return self._cached[1]
        answer = self._ask()
        self._cached = (now, answer)
        return answer

    def _ask(self) -> Tuple[bool, str]:
        if not self.entity_id or not self.allowed_states:
            return False, "presence_not_configured"
        if not self.ha_url or not self.token:
            return False, "presence_no_ha_config"
        try:
            resp = self._get(
                f"{self.ha_url}/api/states/{self.entity_id}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            state = resp.json().get("state")
        except Exception as e:
            return False, f"presence_unreadable:{type(e).__name__}"
        if not isinstance(state, str):
            return False, "presence_unreadable:no_state"
        if state.strip().lower() in self.allowed_states:
            return True, f"presence:{state}"
        return False, f"presence_state:{state}"
