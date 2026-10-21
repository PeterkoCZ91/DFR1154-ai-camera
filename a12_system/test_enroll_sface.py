"""The enrolment tool's side effect on runtime config.

Enrolling has never been only about the gallery file: a name that is not in
`face_recognition.whitelisted_names` is recognised and then alerted about
anyway, so the two have to move together. The dlib-era tool did this and was
deleted with the rest of that path; the capability had to survive it.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.tools.enroll_sface import update_whitelist


def _config(tmp_path):
    return json.loads((tmp_path / "config.json").read_text())


def test_the_whitelist_is_written_from_the_enrolled_names(tmp_path):
    update_whitelist(str(tmp_path), ["Bob", "Alice", "Bob"])
    assert _config(tmp_path)["face_recognition"]["whitelisted_names"] == ["Alice", "Bob"]


def test_a_missing_config_is_created_rather_than_a_crash(tmp_path):
    """First enrolment on a fresh data dir: there is no config.json yet."""
    assert not (tmp_path / "config.json").exists()
    update_whitelist(str(tmp_path), ["Alice"])
    assert _config(tmp_path)["face_recognition"]["whitelisted_names"] == ["Alice"]


def test_unrelated_settings_survive(tmp_path):
    """config.json is the user's runtime override file, not ours to rewrite."""
    (tmp_path / "config.json").write_text(json.dumps({
        "telegram": {"enabled": True},
        "face_recognition": {"cosine_threshold": 0.5},
    }))
    update_whitelist(str(tmp_path), ["Alice"])
    data = _config(tmp_path)
    assert data["telegram"] == {"enabled": True}
    assert data["face_recognition"]["cosine_threshold"] == 0.5
    assert data["face_recognition"]["whitelisted_names"] == ["Alice"]


def test_the_whitelist_is_replaced_not_appended(tmp_path):
    """It must track the gallery. A name with no encodings can never match, so
    leaving it behind only hides that the person was never really enrolled."""
    (tmp_path / "config.json").write_text(json.dumps({
        "face_recognition": {"whitelisted_names": ["Gone", "Alice"]},
    }))
    update_whitelist(str(tmp_path), ["Alice"])
    assert _config(tmp_path)["face_recognition"]["whitelisted_names"] == ["Alice"]


def test_a_malformed_config_is_not_silently_overwritten(tmp_path):
    """Truncating somebody's settings because a byte went wrong is worse than
    refusing: the gallery is already written, so this is recoverable by hand."""
    (tmp_path / "config.json").write_text("{not json")
    try:
        update_whitelist(str(tmp_path), ["Alice"])
    except ValueError:
        pass
    else:
        raise AssertionError("a corrupt config.json must not be overwritten")
    assert (tmp_path / "config.json").read_text() == "{not json"


# --- the tool must run inside the container, where the package has another name


def test_the_tool_imports_under_the_container_package_name(tmp_path):
    import shutil
    import subprocess

    pkg = os.path.dirname(os.path.abspath(__file__))
    shutil.copytree(pkg, tmp_path / "A12_System_v2",
                    ignore=shutil.ignore_patterns("__pycache__", ".git", "*.pkl", "*.onnx"))
    result = subprocess.run(
        [sys.executable, "-c",
         "import A12_System_v2.tools.enroll_sface as m; print(m.write_gallery.__module__)"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "A12_System_v2" in result.stdout


def test_separation_report_says_when_two_people_are_too_close(capsys):
    import numpy as np
    from a12_system.tools.enroll_sface import report_separation

    same = np.ones(128, dtype=np.float32)
    other = np.concatenate([np.ones(64), -np.ones(64)]).astype(np.float32)
    assert report_separation([same, same * 0.99], ["a", "b"]) is True
    assert report_separation([same, other], ["a", "b"]) is False
    assert report_separation([same], ["a"]) is False


def test_existing_people_and_whitelist_preflight(tmp_path):
    from a12_system.face_backend import SFACE_BACKEND
    from a12_system.tools.enroll_sface import existing_people, read_whitelist
    from a12_system.tools.sanitize_gallery import write_gallery

    assert existing_people(str(tmp_path / "none.pkl")) == set()
    gallery = tmp_path / "g.pkl"
    write_gallery(gallery, {"backend": SFACE_BACKEND, "encodings": [[0.0] * 128], "names": ["alice"]})
    assert existing_people(str(gallery)) == {"alice"}

    assert read_whitelist(str(tmp_path)) == []
    (tmp_path / "config.json").write_text('{"face_recognition": {"whitelisted_names": ["alice"]}}')
    assert read_whitelist(str(tmp_path)) == ["alice"]
    (tmp_path / "config.json").write_text("{not json")
    with pytest.raises(ValueError):
        read_whitelist(str(tmp_path))
