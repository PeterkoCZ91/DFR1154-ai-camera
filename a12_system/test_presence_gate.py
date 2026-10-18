"""The optional presence factor fails closed and asks HA at most once per cache."""

import requests

from a12_system.presence_gate import PresenceGate


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._body


def gate(get, states=("home",), entity="person.alice", url="http://ha:8123/", token="tok", clock=None):
    t = clock or {"t": 0.0}
    return PresenceGate(url, token, entity, states, get=get, clock=lambda: t["t"], cache_seconds=8.0)


def test_home_allows_and_the_call_is_well_formed():
    calls = []

    def get(url, **kw):
        calls.append((url, kw))
        return _Resp(body={"state": "home"})

    assert gate(get).allows() == (True, "presence:home")
    url, kw = calls[0]
    assert url == "http://ha:8123/api/states/person.alice"
    assert kw["headers"] == {"Authorization": "Bearer tok"}
    assert kw["timeout"] <= 2.0


def test_states_compare_case_insensitively():
    assert gate(lambda *a, **k: _Resp(body={"state": "Doma"}), states=["home", "doma"]).allows()[0]


def test_any_other_state_denies():
    for state in ("not_home", "away", "unknown", "unavailable", ""):
        assert gate(lambda *a, s=state, **k: _Resp(body={"state": s})).allows()[0] is False


def test_failures_deny():
    def timeout(*a, **k):
        raise requests.Timeout("slow")

    for get in (
        timeout,
        lambda *a, **k: _Resp(401),
        lambda *a, **k: _Resp(404),
        lambda *a, **k: _Resp(body={}),
        lambda *a, **k: _Resp(body={"state": None}),
        lambda *a, **k: _Resp(body=["home"]),
    ):
        allowed, reason = gate(get).allows()
        assert allowed is False and reason.startswith("presence_unreadable")


def test_missing_configuration_denies_without_calling():
    called = []
    get = lambda *a, **k: called.append(1) or _Resp(body={"state": "home"})  # noqa: E731
    assert gate(get, entity="").allows() == (False, "presence_not_configured")
    assert gate(get, states=[]).allows() == (False, "presence_not_configured")
    assert gate(get, url="").allows() == (False, "presence_no_ha_config")
    assert gate(get, token="").allows() == (False, "presence_no_ha_config")
    assert called == []


def test_answers_are_cached_including_failures():
    clock = {"t": 100.0}
    answers = iter([requests.ConnectionError("down"), {"state": "home"}])
    calls = []

    def get(*a, **k):
        calls.append(clock["t"])
        nxt = next(answers)
        if isinstance(nxt, Exception):
            raise nxt
        return _Resp(body=nxt)

    g = gate(get, clock=clock)
    assert g.allows()[0] is False
    clock["t"] += 5.0
    assert g.allows()[0] is False   # cached failure: HA is not asked again
    clock["t"] += 4.0
    assert g.allows()[0] is True    # cache expired, asked again
    assert calls == [100.0, 109.0]
