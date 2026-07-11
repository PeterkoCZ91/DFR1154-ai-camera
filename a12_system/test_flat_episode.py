import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.flat_episode import FlatEpisodeState, prune_files


def _state(tmp_path):
    return FlatEpisodeState(str(tmp_path / "flat_episode_state.json"))


def test_should_notify_rate_limits_per_key(tmp_path):
    s = _state(tmp_path)
    assert s.should_notify("restart", now=1000.0, min_interval=3600) is True
    assert s.should_notify("restart", now=2000.0, min_interval=3600) is False
    assert s.should_notify("restart", now=5000.0, min_interval=3600) is True


def test_keys_are_independent(tmp_path):
    s = _state(tmp_path)
    assert s.should_notify("restart", now=1000.0, min_interval=3600) is True
    assert s.should_notify("flat_alert", now=1000.0, min_interval=3600) is True


def test_rate_limit_survives_process_restart(tmp_path):
    # 47 container restarts overnight must not mean 47 Telegram messages.
    path = tmp_path / "flat_episode_state.json"
    FlatEpisodeState(str(path)).should_notify("restart", now=1000.0, min_interval=3600)
    fresh_process = FlatEpisodeState(str(path))
    assert fresh_process.should_notify("restart", now=1100.0, min_interval=3600) is False


def test_episode_lifecycle_recovery_fires_once(tmp_path):
    s = _state(tmp_path)
    assert s.episode_active() is False
    s.mark_active()
    assert s.episode_active() is True
    assert s.clear() is True   # recovery -> caller sends "recovered" message
    assert s.clear() is False  # already cleared -> no duplicate message


def test_corrupt_state_file_treated_as_empty(tmp_path):
    path = tmp_path / "flat_episode_state.json"
    path.write_text("{not json")
    s = FlatEpisodeState(str(path))
    assert s.episode_active() is False
    assert s.should_notify("restart", now=1000.0, min_interval=3600) is True


def test_prune_files_keeps_newest(tmp_path):
    for i in range(7):
        f = tmp_path / f"flat_debug_raw_{i}.jpg"
        f.write_bytes(b"x")
        os.utime(f, (1000 + i, 1000 + i))
    (tmp_path / "unrelated.jpg").write_bytes(b"x")
    prune_files(str(tmp_path), "flat_debug_raw_", keep=3)
    kept = sorted(p.name for p in tmp_path.glob("flat_debug_raw_*"))
    assert kept == ["flat_debug_raw_4.jpg", "flat_debug_raw_5.jpg", "flat_debug_raw_6.jpg"]
    assert (tmp_path / "unrelated.jpg").exists()


def test_reboot_budget_survives_process_restart(tmp_path):
    # A crash-looping A12 must not re-arm 5 fresh camera reboots per process.
    path = tmp_path / "flat_episode_state.json"
    s = FlatEpisodeState(str(path))
    s.mark_active()
    assert s.record_reboot() == 1
    assert s.record_reboot() == 2
    fresh_process = FlatEpisodeState(str(path))
    assert fresh_process.reboot_count() == 2
    assert fresh_process.record_reboot() == 3


def test_gaveup_latch_fires_once_across_restarts(tmp_path):
    path = tmp_path / "flat_episode_state.json"
    s = FlatEpisodeState(str(path))
    s.mark_active()
    assert s.set_gaveup() is True    # first give-up -> alert
    assert s.set_gaveup() is False   # same episode -> no duplicate
    fresh_process = FlatEpisodeState(str(path))
    assert fresh_process.set_gaveup() is False  # A12 restart -> still no duplicate


def test_clear_rearms_ladder_for_next_episode(tmp_path):
    # A real recovery ends the episode: the next genuine hang gets a fresh
    # reboot budget, give-up latch and an immediate onset alert.
    s = _state(tmp_path)
    s.mark_active()
    s.record_reboot()
    s.set_gaveup()
    assert s.should_notify("flat_alert", now=1000.0, min_interval=3600) is True
    assert s.clear() is True
    assert s.reboot_count() == 0
    s.mark_active()
    assert s.reboot_count() == 0
    assert s.should_notify("flat_alert", now=1500.0, min_interval=3600) is True
    assert s.set_gaveup() is True


def test_other_notify_keys_survive_clear(tmp_path):
    # "recovered"/"camera_reboot" timestamps persist across episodes so a slow
    # flapping stream stays rate-limited.
    s = _state(tmp_path)
    s.mark_active()
    assert s.should_notify("recovered", now=1000.0, min_interval=3600) is True
    s.clear()
    assert s.should_notify("recovered", now=1100.0, min_interval=3600) is False


def test_rate_limit_survives_unwritable_state_file(tmp_path, monkeypatch):
    # Disk full (/data unwritable) must NOT degrade rate-limiting into
    # "notify every time" — the in-memory mirror keeps the process honest.
    s = _state(tmp_path)
    monkeypatch.setattr(
        "a12_system.flat_episode.os.replace",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert s.should_notify("camera_reboot", now=1000.0, min_interval=3600) is True
    assert s.should_notify("camera_reboot", now=1100.0, min_interval=3600) is False
    s.mark_active()
    assert s.episode_active() is True
