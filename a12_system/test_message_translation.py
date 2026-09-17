"""Alerts leave in the language the operator picked in the camera's web UI.

Translation happens on the way out, in Notifier.send_telegram, not at the call
sites. That is deliberate: the pipeline keeps producing English, so the 25
assertions across the suite that pin down what an alert *says* — several of
them written because a message was misleading and had to be reworded — keep
testing the sentence the code chose, not the language it was rendered in.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.messages import CZECH, translate, translate_outgoing
from a12_system.notifier import Notifier

SAMPLE = "Stream recovered — frames are healthy again."


def test_czech_is_the_default_when_no_language_is_known():
    assert translate(SAMPLE, "cz") == CZECH[SAMPLE]


def test_english_passes_through_untouched():
    assert translate(SAMPLE, "en") == SAMPLE


def test_an_uncatalogued_message_is_sent_as_written():
    """A new alert must reach the operator, not vanish because nobody translated it."""
    novel = "Something nobody has translated yet"
    assert translate(novel, "cz") == novel
    assert translate(novel, "en") == novel


def test_an_unknown_language_falls_back_to_the_source_text():
    assert translate(SAMPLE, "klingon") == SAMPLE


def test_placeholders_survive_translation():
    key = "Camera stream lost ({}s) — reconnecting..."
    assert key in CZECH
    assert translate(key, "cz").count("{}") == key.count("{}")
    assert translate(key, "cz").format(42) != key.format(42)


# --- the notifier applies it, and only on the way out ---


class _Bot:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, message):
        self.sent.append(message)


def _notifier(language):
    n = object.__new__(Notifier)
    n.config = {"telegram_cooldown_seconds": 0}
    n.bot = _Bot()
    n.chat_id = "1"
    n.last_telegram_time = 0
    n._rate_limited_until = 0.0
    import threading
    n._send_lock = threading.Lock()
    n.language_source = lambda: language
    return n


def test_the_notifier_translates_on_the_way_out():
    n = _notifier("cz")
    assert n.send_telegram(SAMPLE, bypass_cooldown=True) is True
    assert n.bot.sent == [CZECH[SAMPLE]]


def test_english_reaches_telegram_unchanged():
    n = _notifier("en")
    n.send_telegram(SAMPLE, bypass_cooldown=True)
    assert n.bot.sent == [SAMPLE]


def test_a_labelled_message_still_translates():
    """The camera label is prepended before the notifier sees it."""
    n = _notifier("cz")
    n.send_telegram(f"ESP32 AI Camera: {SAMPLE}", bypass_cooldown=True)
    assert n.bot.sent == [f"ESP32 AI Camera: {CZECH[SAMPLE]}"]


def test_no_language_source_configured_still_sends():
    """A Notifier built before the camera exists must not crash on the first alert."""
    n = _notifier("cz")
    n.language_source = None
    n.send_telegram(SAMPLE, bypass_cooldown=True)
    assert n.bot.sent == [CZECH[SAMPLE]]


def test_a_failing_language_source_does_not_lose_the_alert():
    def boom():
        raise RuntimeError("camera gone")

    n = _notifier("cz")
    n.language_source = boom
    assert n.send_telegram(SAMPLE, bypass_cooldown=True) is True
    assert len(n.bot.sent) == 1


# --- messages that carry interpolated values ---
#
# 14 of the 36 catalogue entries contain {}. An exact dict lookup can never
# match those, because by the time the notifier sees the message the values are
# already substituted — "Camera stream lost (42s)" is not the key. Caught when
# the startup alert went out in English on a camera set to Czech.


def test_a_message_with_an_interpolated_value_is_translated():
    sent = "Camera stream lost (42s) — reconnecting..."
    out = translate_outgoing(sent, "cz")
    assert out != sent
    assert "42" in out


def test_the_value_itself_is_not_translated_or_reordered():
    sent = "ESP32 Health Warning: camera_init_failed"
    out = translate_outgoing(sent, "cz")
    assert "camera_init_failed" in out


def test_several_values_keep_their_order():
    key = "Faces (24h): {} known / {} strangers / {} with no readable face"
    assert key in CZECH
    # Distinctive values: the label itself contains "24h", so a digit like 2
    # would be found inside the heading rather than in the slot under test.
    out = translate_outgoing(key.format(71, 83, 95), "cz")
    assert out.index("71") < out.index("83") < out.index("95")


def test_a_longer_template_wins_over_a_shorter_one_it_contains():
    """'{} detected' is a prefix of '{} detected (Video)'.

    Matching the short one first would render "Person detected (Video)" as
    "Detekce: Person (Video)" — the wrong template, silently.
    """
    out = translate_outgoing("Person detected (Video)", "cz")
    assert out == CZECH["{} detected (Video)"].format("Person")


def test_a_labelled_message_with_a_value_still_translates():
    sent = "ESP32 AI Camera: Camera stream lost (9s) — reconnecting..."
    out = translate_outgoing(sent, "cz")
    assert out.startswith("ESP32 AI Camera: ")
    assert "9" in out
    assert "reconnecting" not in out


def test_english_leaves_an_interpolated_message_alone():
    sent = "Camera stream lost (42s) — reconnecting..."
    assert translate_outgoing(sent, "en") == sent


def test_a_message_matching_nothing_is_sent_as_written():
    sent = "Completely novel alert with a number 7 in it"
    assert translate_outgoing(sent, "cz") == sent


def test_a_multiline_message_with_values_is_translated():
    key = "A12 resource warning\nRAM: {}/{} MB ({}%)\nConsider reducing clip buffer or raising mem_limit in compose."
    assert key in CZECH
    out = translate_outgoing(key.format(900, 1024, 88), "cz")
    assert out != key.format(900, 1024, 88)
    assert "900" in out and "1024" in out and "88" in out
