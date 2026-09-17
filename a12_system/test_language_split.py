"""Czech belongs in what the system says to its operator, nothing else.

The repository is public and its own voice — comments, docstrings, docs, the
changelog, commit messages — is English. The exception is real and deliberate:
the daily summary this system sends to a Czech-speaking operator is written in
Czech, and the test that asserts on it has to quote it verbatim.

That split existed in practice but was written down nowhere, so new code
followed it by accident. This test makes it a rule. When it fires, the question
is which side the text is on: is it something a user reads as output, or
something a reader of the repository reads as explanation?

What it does and does not prove: in Python it is exact, because the Czech has
to sit inside a string token, so a Czech comment in an allowlisted file still
fails. In Markdown the allowlist is per file — the two entries there quote the
product's own Czech strings, and nothing checks that a future edit stays a
quotation. That part still rests on review; the guard is
`test_the_allowlist_does_not_rot`, which at least forces every exemption to
still be doing something.
"""

import io
import os
import tokenize

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CZECH = set("ěščřžýáíéůúňťďóĚŠČŘŽÝÁÍÉŮÚŇŤĎÓ")

SCANNED_SUFFIXES = (".py", ".md", ".cpp", ".h", ".sh", ".html")

# Directories this repository does not write.
SKIPPED_DIRS = {
    ".git", "__pycache__", "node_modules", ".pio", "logs", "recordings",
    # Vendored third-party sources, and a vendored skills bundle.
    os.path.join("firmware", "lib"),
    os.path.join("docs", "superpowers"),
    # The owner's own working instructions for their tooling. Same side of the
    # line as the daily summary: written for the person who runs this, not for
    # a reader trying to understand the project. Nothing in the published
    # interface is explained here.
    ".claude",
}

# Files allowed to contain Czech, each with the reason it is allowed. In a
# Python file the Czech must additionally sit inside a string literal: a label
# the operator reads is output, a comment in Czech is not.
ALLOWED = {
    "a12_system/status_monitor.py": "labels of the daily summary sent to the operator",
    "a12_system/test_learning_data.py": "asserts on that summary's text verbatim",
    "a12_system/test_language_split.py": "names the characters it searches for",
    "README.md": "quotes the dashboard's own Czech button labels",
    "CHANGELOG.md": "quotes the daily summary's own Czech line",
}


def _tracked_files():
    for dirpath, dirnames, filenames in os.walk(REPO):
        rel_dir = os.path.relpath(dirpath, REPO)
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIPPED_DIRS
            and os.path.normpath(os.path.join(rel_dir, d)) not in SKIPPED_DIRS
        ]
        for name in filenames:
            if not name.endswith(SCANNED_SUFFIXES) or name.startswith("LOCAL_"):
                continue
            yield os.path.normpath(os.path.join(rel_dir, name))


def _czech_lines(path):
    try:
        text = open(os.path.join(REPO, path), encoding="utf-8").read()
    except (UnicodeDecodeError, OSError):
        return []
    return [
        (n, line.strip()[:90])
        for n, line in enumerate(text.splitlines(), 1)
        if CZECH & set(line)
    ]


def _czech_outside_strings(path):
    """Czech in a .py file that is not inside a string literal."""
    source = open(os.path.join(REPO, path), encoding="utf-8").read()
    offenders = []
    # Since 3.12 an f-string is not a STRING token but a FSTRING_START /
    # FSTRING_MIDDLE / FSTRING_END triple, so checking STRING alone reports
    # every f-string label as a bare comment. getattr keeps this working on
    # older interpreters, where those token types do not exist.
    string_types = {tokenize.STRING} | {
        getattr(tokenize, name)
        for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
        if hasattr(tokenize, name)
    }
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type in string_types:
                continue
            if CZECH & set(tok.string):
                offenders.append((tok.start[0], tok.string.strip()[:90]))
    except (tokenize.TokenError, IndentationError):
        pass
    return offenders


def test_czech_appears_only_where_it_is_allowed():
    offenders = {
        path: lines
        for path in sorted(_tracked_files())
        if path not in ALLOWED and (lines := _czech_lines(path))
    }
    assert offenders == {}, (
        "Czech outside the operator-facing output it is reserved for:\n"
        + "\n".join(
            f"  {p}:{n}  {t}" for p, lines in offenders.items() for n, t in lines
        )
    )


def test_allowed_python_files_keep_czech_inside_strings():
    """An allowlisted file is not a free pass for Czech comments."""
    offenders = {
        path: lines
        for path in ALLOWED
        if path.endswith(".py") and (lines := _czech_outside_strings(path))
    }
    assert offenders == {}, f"Czech outside a string literal: {offenders}"


def test_the_allowlist_does_not_rot():
    """Every exemption must still be needed, and must still exist."""
    for path, reason in ALLOWED.items():
        full = os.path.join(REPO, path)
        assert os.path.isfile(full), f"allowlisted file is gone: {path}"
        assert reason, f"no reason recorded for {path}"
        assert _czech_lines(path), (
            f"{path} no longer contains Czech — drop it from the allowlist "
            "rather than leaving an exemption nothing uses"
        )
