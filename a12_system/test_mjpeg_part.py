"""Tests for MJPEG part-header parsing.

Regression cover for the parser that used to scan only for JPEG SOI/EOI markers.
The camera's socket send timeout really does cut frames short, and marker-only
scanning silently concatenated the truncated frame with the next one — OpenCV then
decoded the mangled result into a plausible-looking corrupt image, which fed the
flat/dark frame watchdog and could escalate to a spurious camera reboot.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.camera import _part_content_length


def _part(payload_len: int, extra_headers: bytes = b"") -> bytes:
    return (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: " + str(payload_len).encode() + b"\r\n"
        + extra_headers +
        b"\r\n"
    )


def test_reads_declared_length():
    buf = _part(1234) + b"\xff\xd8payload"
    soi = buf.find(b"\xff\xd8")
    assert _part_content_length(buf, soi) == 1234


def test_reads_length_with_extra_headers_present():
    # The CamS3 firmware adds X-Timestamp / X-Frame-Age after Content-Length.
    extra = b"X-Timestamp: 123456\r\nX-Frame-Age: 12\r\n"
    buf = _part(4096, extra) + b"\xff\xd8payload"
    soi = buf.find(b"\xff\xd8")
    assert _part_content_length(buf, soi) == 4096


def test_header_case_is_ignored():
    buf = b"--frame\r\nCONTENT-LENGTH: 77\r\n\r\n\xff\xd8x"
    soi = buf.find(b"\xff\xd8")
    assert _part_content_length(buf, soi) == 77


def test_picks_the_nearest_header_not_the_first():
    # Two parts in the buffer: the length for the second must not come from the first.
    first = _part(11) + b"\xff\xd8" + b"a" * 9
    second = _part(22) + b"\xff\xd8" + b"b" * 20
    buf = first + second
    soi = buf.find(b"\xff\xd8", len(first))
    assert _part_content_length(buf, soi) == 22


def test_missing_header_returns_none():
    buf = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8payload"
    soi = buf.find(b"\xff\xd8")
    assert _part_content_length(buf, soi) is None


def test_unparsable_value_returns_none():
    buf = b"--frame\r\nContent-Length: not-a-number\r\n\r\n\xff\xd8x"
    soi = buf.find(b"\xff\xd8")
    assert _part_content_length(buf, soi) is None


def test_absurd_value_returns_none():
    # A misread header must fall back to marker scanning rather than stall the
    # parser waiting for bytes that will never arrive.
    for bogus in (b"0", b"-5", b"99999999"):
        buf = b"--frame\r\nContent-Length: " + bogus + b"\r\n\r\n\xff\xd8x"
        soi = buf.find(b"\xff\xd8")
        assert _part_content_length(buf, soi) is None, bogus


def test_header_beyond_lookbehind_is_not_used():
    padding = b"#" * 900
    buf = _part(64) + padding + b"\xff\xd8x"
    soi = buf.find(b"\xff\xd8")
    assert _part_content_length(buf, soi) is None


def test_soi_at_buffer_start_does_not_underflow():
    buf = b"\xff\xd8payload"
    assert _part_content_length(buf, 0) is None
