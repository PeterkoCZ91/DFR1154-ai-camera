"""Nuki Hub's own answers: the action verdict, the direct MQTT path when Home
Assistant is down, and the watch that says when the hub or lock is gone."""

from types import SimpleNamespace

from a12_system.door_unlock import ha_unlock, mqtt_unlatch
from a12_system.nuki_log import NukiActivity, NukiHubWatch
from a12_system.test_door_unlock import _own_commands, _Resp


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def _unchanged_ha(state="unlocked"):
    # The HA state never moves: only Nuki Hub's verdict can confirm.
    return dict(post=lambda url, **kw: _Resp(200), get=lambda url, **kw: _Resp(200, {"state": state}))


def test_nuki_hubs_success_confirms_between_ha_reads():
    clock = _Clock()
    verdicts = {}
    ok, detail = ha_unlock(
        "http://ha", "t", "lock.x", sleep=lambda s: (clock.sleep(s), verdicts.setdefault("at", clock.t)),
        clock=clock, settle_seconds=10, command_result=lambda since: "success" if clock.t >= since + 0.5 else None,
        **_unchanged_ha(),
    )
    assert (ok, detail) == (True, "unlocked/command_success")
    assert clock.t - 1000.0 < 1.0  # not a whole HA poll later


def test_a_failed_attempt_is_waited_out_and_the_lock_log_overrules():
    clock = _Clock()
    ok, detail = ha_unlock(
        "http://ha", "t", "lock.x", sleep=clock.sleep, clock=clock, settle_seconds=3,
        command_result=lambda since: "failed", **_unchanged_ha(),
    )
    assert (ok, detail) == (False, "open_not_confirmed:unlocked")
    ok, detail = ha_unlock(
        "http://ha", "t", "lock.x", sleep=clock.sleep, clock=clock, settle_seconds=3,
        command_result=lambda since: "success",
        lock_log=lambda since: ("busy", None) if clock.t > since + 0.2 else (None, None),
        **_unchanged_ha(),
    )
    assert (ok, detail) == (False, "open_refused:busy")


def test_only_a_verdict_after_the_call_counts():
    clock = _Clock()
    activity = NukiActivity(clock=clock)
    activity.observe_command_result("success")
    assert activity.command_result_since(clock.t + 0.1) is None
    clock.t += 1.0
    activity.observe_command_result("timeOut")
    assert activity.command_result_since(clock.t - 0.5) == "timeOut"


def test_the_direct_path_publishes_unlatch_and_waits_for_success():
    clock = _Clock()
    sent = []
    answers = {1001.0: "failed", 1002.0: "success"}

    def result(since):
        return next((v for t, v in sorted(answers.items(), reverse=True) if clock.t >= t), None)

    ok, detail = mqtt_unlatch(lambda action: sent.append(action) or True, result, sleep=clock.sleep, clock=clock)
    assert sent == ["unlatch"] and (ok, detail) == (True, "mqtt/command_success")
    assert mqtt_unlatch(lambda action: False, result, sleep=clock.sleep, clock=clock) == (False, "mqtt_unavailable")
    ok, detail = mqtt_unlatch(lambda a: True, lambda since: None, sleep=clock.sleep, clock=clock, settle_seconds=1)
    assert (ok, detail) == (False, "mqtt_not_confirmed:None")


def _report_pipeline(monkeypatch, ha_answer, mqtt_answer=(True, "mqtt/command_success")):
    from a12_system import pipeline

    sent, published, events = [], [], []
    fake = SimpleNamespace(
        ha_url="http://ha", ha_token="t", nuki_entity_id="lock.x", log_prefix="[t]",
        db=SimpleNamespace(log_event=lambda *a: events.append(a)),
        notifier=SimpleNamespace(send_telegram=lambda text, **kw: sent.append(text) or True),
        _allow_face_unlock_retry=lambda: None,
        nuki_activity=NukiActivity(),
        mqtt_client=SimpleNamespace(publish_nuki_action=lambda a: published.append(a) or True),
        **_own_commands(),
    )
    monkeypatch.setattr(pipeline, "ha_unlock", lambda *a, **k: ha_answer)
    monkeypatch.setattr(pipeline, "mqtt_unlatch", lambda publish, result: (publish("unlatch"), mqtt_answer)[1])
    pipeline.DetectionPipeline._nuki_unlock_report(fake, "alice", 1)
    return sent, published, events


def test_ha_down_opens_through_nuki_hub(monkeypatch):
    sent, published, events = _report_pipeline(monkeypatch, (False, "service_call_failed:connection refused"))
    assert published == ["unlatch"]
    assert len(sent) == 1 and "confirmed" in sent[0] and "NOT" not in sent[0]
    assert events[0][0] == "unlock"


def test_a_lock_problem_is_not_retried_around_ha(monkeypatch):
    # HA answered: the lock is lost or busy, and going around HA changes nothing.
    for detail in ("lock_unavailable", "open_refused:motorBlocked", "open_not_confirmed:unlocked"):
        sent, published, _ = _report_pipeline(monkeypatch, (False, detail))
        assert published == [] and "NOT" in sent[0]


def test_the_watch_reports_each_outage_once_and_its_end():
    clock = _Clock()
    watch = NukiHubWatch(300.0, clock=clock)
    clock.t += 200
    watch.observe("nukihub/maintenance/uptime", "51")
    clock.t += 290
    assert watch.check() == []
    clock.t += 20
    assert watch.check() == [("hub_silent", 5)]
    clock.t += 600
    assert watch.check() == []  # told once
    watch.observe("nukihub/maintenance/uptime", "1")
    assert watch.check() == [("hub_back", 15)]

    watch.observe("nukihub/lock/availability", "offline")
    clock.t += 299
    watch.observe("nukihub/maintenance/uptime", "6")
    assert watch.check() == []
    clock.t += 2
    watch.observe("nukihub/lock/availability", "offline")  # repeated: still the same outage
    assert watch.check() == [("lock_lost", 5)]
    assert watch.check() == []
    watch.observe("nukihub/lock/availability", "online")
    assert watch.check() == [("lock_back", 5)]


def test_a_short_blip_and_a_disabled_watch_say_nothing():
    clock = _Clock()
    watch = NukiHubWatch(300.0, clock=clock)
    watch.observe("nukihub/lock/availability", "offline")
    clock.t += 60
    watch.observe("nukihub/lock/availability", "online")
    watch.observe("nukihub/maintenance/uptime", "9")
    assert watch.check() == []
    off = NukiHubWatch(0.0, clock=clock)
    clock.t += 10_000
    assert off.check() == []


def test_the_status_monitor_sends_the_watch_events():
    from a12_system.messages import CZECH
    from a12_system.status_monitor import StatusMonitor

    sent = []
    fake = SimpleNamespace(
        nuki_watch=SimpleNamespace(check=lambda now: [("hub_silent", 7), ("lock_back", 3)]),
        notifier=SimpleNamespace(send_telegram=lambda text, **kw: sent.append((text, kw))),
    )
    StatusMonitor._check_nuki_hub(fake, 0.0)
    assert [t for t, _ in sent] == [
        "Nuki Hub has been silent for 7 min: the door opens neither by face nor by /od.",
        "Nuki Hub reaches the lock again (lost for 3 min).",
    ]
    assert all(kw.get("critical") for _, kw in sent)
    keys = [t.replace("7", "{}").replace("3", "{}") for t, _ in sent]
    assert all(k in CZECH for k in keys)


def test_state_topics_follow_the_log_topic():
    from a12_system.mqtt_client import MQTTClient

    c = MQTTClient.__new__(MQTTClient)
    c.config = {"mqtt": {"nuki_log_topic": "nukihub/lock/log"}}
    assert c._nuki_state_topics() == (
        "nukihub/lock/commandResult", "nukihub/lock/availability", "nukihub/maintenance/uptime",
    )
    c.config = {"mqtt": {"nuki_log_topic": ""}}
    assert c._nuki_state_topics() == ()
