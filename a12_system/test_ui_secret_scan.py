"""The scan must actually catch the thing that got through.

A green scan proves nothing on its own — the question is what it would print if
the leak were still there. These tests put it back and check.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCANNER = os.path.join(REPO, "tools", "scan_ui_secrets.py")


def _scan(tmp_path, html: str):
    path = tmp_path / "page.html"
    path.write_text(html, encoding="utf-8")
    return subprocess.run(
        [sys.executable, SCANNER, str(path)], capture_output=True, text=True
    )


def test_a_bare_long_digit_run_as_a_placeholder_is_caught(tmp_path):
    """The shape of the line that shipped for four months.

    Not the value: a test that proves the scanner catches the leaked chat ID by
    writing the leaked chat ID puts it back in the repository, which is what we
    just spent the afternoon removing. Any nine-digit number exercises the same
    branch.
    """
    result = _scan(tmp_path, '<input type="text" id="tg_chat" placeholder="555000111">')
    assert result.returncode == 1
    assert "555000111" in result.stderr
    assert "9-digit" in result.stderr


def test_the_generic_example_that_replaced_it_passes(tmp_path):
    result = _scan(tmp_path, '<input type="text" id="tg_chat" placeholder="123456789">')
    assert result.returncode == 0


def test_a_bot_token_is_caught_anywhere_in_the_line(tmp_path):
    result = _scan(tmp_path, '<p>token 8333277223:AAF_this_is_thirty_chars_of_junk_x</p>')
    assert result.returncode == 1
    assert "bot token" in result.stderr.lower()


def test_the_documented_token_example_is_not_a_finding(tmp_path):
    result = _scan(tmp_path, '<input placeholder="123456789:ABCdef...">')
    assert result.returncode == 0


def test_ordinary_ui_values_are_not_findings(tmp_path):
    result = _scan(
        tmp_path,
        '<input value="12" min="0" max="63">\n<input placeholder="192.168.1.100">\n'
        '<option value="10">XGA</option>',
    )
    assert result.returncode == 0, result.stderr


def test_an_unreadable_file_fails_loudly_rather_than_passing_empty(tmp_path):
    """A scan that read nothing must not report success.

    The first version of this test passed for the wrong reason: it handed the
    scanner a path that did not exist, the open() raised, and the traceback
    exited 1. That would have stayed green even if the scanner reported
    "clean" for a file it never opened.
    """
    result = subprocess.run(
        [sys.executable, SCANNER, str(tmp_path / "does_not_exist.html")],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "cannot read" in result.stderr
    assert "Traceback" not in result.stderr


def test_the_shipped_ui_sources_are_clean():
    result = subprocess.run([sys.executable, SCANNER], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
