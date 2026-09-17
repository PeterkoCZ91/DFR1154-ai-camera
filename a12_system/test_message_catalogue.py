"""Every operator-visible literal must have a translation, or the catalogue rots.

Committing a catalogue is not enough — the same lesson as the web UI sources.
Without something that fails, the next alert someone adds goes out in English
while everything around it is Czech, and nobody notices until an operator asks
why half the messages changed language.

This reads the call sites rather than a list somebody maintains by hand: it
parses the source, resolves each argument passed to send_telegram() and
_telegram_message() into the literal skeleton it produces, and requires an
entry for it. A message assembled from runtime values alone has nothing to
translate and is skipped.
"""

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.messages import CZECH

ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCES = ("pipeline.py", "status_monitor.py", "__main__.py", "notifier.py")
SENDERS = ("send_telegram", "_telegram_message", "_caption")


def _skeleton(node):
    """The literal text a call argument produces, with {} where a value goes."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _skeleton(node.left), _skeleton(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.IfExp):
        # `a if cond else b` — both branches are real call sites.
        return [_skeleton(node.body), _skeleton(node.orelse)]
    if isinstance(node, ast.Call):
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name in SENDERS and node.args:
            return _skeleton(node.args[0])
        if name == "format" and isinstance(node.func, ast.Attribute):
            return _skeleton(node.func.value)
    return None


def _translatable(text) -> bool:
    """Is there anything here to translate?

    `f"{label}: {msg}"` reduces to "{}: {}" — punctuation and placeholders, no
    words. Demanding a translation for that would be noise.
    """
    return bool(text) and any(ch.isalpha() for ch in text)


def _local_literals(tree, built):
    """{variable name: [literal, ...]} for strings built up before being sent.

    Several alerts are assembled into a variable first — `msg = "..."`,
    `startup_msg += "..."` — so looking only at the call argument would miss
    them, and a new one added that way would slip past this test unnoticed.
    """
    assigned = {}
    for node in ast.walk(tree):
        targets, value = [], None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
            targets, value = [node.target], node.value
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            text = _skeleton(value)
            pieces = text if isinstance(text, list) else [text]
            # `startup_msg = _startup_message(...)` — the literal lives in the
            # builder, so follow the call rather than recording nothing.
            if isinstance(value, ast.Call):
                callee = getattr(value.func, "attr", None) or getattr(value.func, "id", None)
                pieces = pieces + built.get(callee, [])
            for piece in pieces:
                if _translatable(piece):
                    assigned.setdefault(target.id, []).append(piece)
    return assigned


def _returned_literals(tree):
    """{function name: [literal, ...]} for helpers that build a message.

    `_startup_message()` composes its text and returns it, so the call site
    passes a Call, not a string. Without this the startup message would look
    untranslated to this test and the catalogue entry for it would look unused
    — both wrong, and in opposite directions.
    """
    built = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.endswith("_message"):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Return) and inner.value is not None:
                text = _skeleton(inner.value)
                for piece in text if isinstance(text, list) else [text]:
                    if _translatable(piece):
                        built.setdefault(node.name, []).append(piece)
    return built


# The daily summary is composed from parts that are each translated as they are
# added, so its finished skeleton is an artefact of the assembly rather than a
# message anyone wrote. The only word left in it is "Uptime", which is the same
# in both languages.
ASSEMBLED = {"{}\nUptime: {}\n{}\n{}"}


def _literals():
    """{literal: [file:line, ...]} for every operator-visible string sent."""
    found = {}
    for filename in SOURCES:
        path = os.path.join(ROOT, filename)
        tree = ast.parse(open(path, encoding="utf-8").read(), path)
        built = _returned_literals(tree)
        assigned = _local_literals(tree, built)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in SENDERS or not node.args:
                continue
            argument = node.args[0]
            result = _skeleton(argument)
            texts = result if isinstance(result, list) else [result]
            if not any(_translatable(t) for t in texts) and isinstance(argument, ast.Name):
                texts = assigned.get(argument.id, [])
            if isinstance(argument, ast.Call):
                callee = getattr(argument.func, "attr", None) or getattr(argument.func, "id", None)
                texts = texts + built.get(callee, [])
            for text in texts:
                if _translatable(text) and text not in ASSEMBLED:
                    found.setdefault(text, []).append(f"{filename}:{node.lineno}")
    return found


def test_every_sent_literal_has_a_translation():
    untranslated = {
        text: sites for text, sites in _literals().items() if text not in CZECH
    }
    assert untranslated == {}, (
        "operator-visible text with no entry in messages.py:\n"
        + "\n".join(f"  {sites[0]}  {text!r}" for text, sites in untranslated.items())
    )


def test_the_scan_actually_finds_the_call_sites():
    """Guards the test above from passing because it found nothing at all."""
    literals = _literals()
    assert len(literals) >= 15, f"only {len(literals)} literals found — the scan broke"
    assert "Stream recovered — frames are healthy again." in literals


def test_placeholder_counts_match_between_languages():
    """A missing {} raises IndexError at format time, in the alert path."""
    mismatched = {
        key: value for key, value in CZECH.items()
        if key.count("{}") != value.count("{}")
    }
    assert mismatched == {}


def test_no_translation_is_empty():
    assert [key for key, value in CZECH.items() if not value.strip()] == []


def test_catalogue_has_no_entry_for_text_nobody_sends():
    """An unused key is dead weight and hides a call site that was reworded.

    Exempt: labels the daily summary assembles, which reach the catalogue
    through translate() rather than through a send_telegram() argument.
    """
    literals = set(_literals())
    assembled = {
        "A12 daily summary", "Events (24h):", "(none)", "PIR/HA triggers",
        "Local clips", "Confirmed person", "Dog", "Audio alerts",
        "Stream interruptions",
        "Scorer (since start): {} OK / {} errors / {} fallbacks, p95 {}s",
        "Faces (24h): {} known / {} strangers / {} with no readable face",
    }
    orphans = sorted(set(CZECH) - literals - assembled)
    assert orphans == [], f"catalogue entries nothing sends: {orphans}"
