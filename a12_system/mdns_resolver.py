"""Small mDNS fallback resolver for .local camera hostnames."""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import threading
import time
from urllib.parse import urlsplit, urlunsplit

_MDNS_ADDR = "224.0.0.251"
_MDNS_PORT = 5353
_CACHE_TTL_SECONDS = 60
_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


def _cache_path() -> str | None:
    data_dir = os.environ.get("A12_DATA_DIR") or os.environ.get("A12_CONFIG_DIR")
    if not data_dir:
        return None
    return os.path.join(data_dir, "mdns_cache.json")


def _load_persistent_cache(key: str) -> str | None:
    path = _cache_path()
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    except FileNotFoundError:
        return None
    except Exception as exc:
        logging.debug("Failed to read mDNS cache %s: %s", path, exc)
    return None


def _save_persistent_cache(key: str, ip: str) -> None:
    path = _cache_path()
    if not path:
        return
    try:
        data = {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                data = loaded
        except FileNotFoundError:
            pass
        data[key] = ip
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
        os.replace(tmp_path, path)
    except Exception as exc:
        logging.debug("Failed to write mDNS cache %s: %s", path, exc)


def _encode_name(hostname: str) -> bytes:
    labels = hostname.rstrip(".").split(".")
    return b"".join(bytes([len(label)]) + label.encode("ascii") for label in labels) + b"\x00"


def _skip_name(packet: bytes, offset: int) -> int:
    while True:
        if offset >= len(packet):
            raise ValueError("DNS name exceeds packet length")
        length = packet[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += 1 + length


def _parse_ipv4_answer(packet: bytes) -> tuple[str, int] | None:
    if len(packet) < 12:
        return None

    _txid, flags, qdcount, ancount, nscount, arcount = struct.unpack_from("!HHHHHH", packet, 0)
    if not flags & 0x8000:
        return None

    offset = 12
    for _ in range(qdcount):
        offset = _skip_name(packet, offset)
        offset += 4

    for _ in range(ancount + nscount + arcount):
        offset = _skip_name(packet, offset)
        if offset + 10 > len(packet):
            return None
        rr_type, rr_class, ttl, rdlength = struct.unpack_from("!HHIH", packet, offset)
        offset += 10
        if offset + rdlength > len(packet):
            return None
        rdata = packet[offset : offset + rdlength]
        offset += rdlength

        if rr_type == 1 and (rr_class & 0x7FFF) == 1 and rdlength == 4:
            return socket.inet_ntoa(rdata), int(ttl) or _CACHE_TTL_SECONDS

    return None


def _query_mdns_ipv4(hostname: str, timeout: float = 2.0, attempts: int = 3) -> tuple[str, int] | None:
    question = _encode_name(hostname)
    packet = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + question + struct.pack("!HH", 1, 1)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)

        for _attempt in range(max(1, attempts)):
            deadline = time.monotonic() + timeout
            sock.sendto(packet, (_MDNS_ADDR, _MDNS_PORT))

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    response, _addr = sock.recvfrom(2048)
                except TimeoutError:
                    break
                except socket.timeout:
                    break

                answer = _parse_ipv4_answer(response)
                if answer:
                    return answer

    return None


def resolve_host(hostname: str, *, force: bool = False) -> str:
    """Resolve a .local hostname through mDNS, falling back to the original host."""
    host = (hostname or "").strip().rstrip(".")
    if not host.lower().endswith(".local"):
        return hostname

    key = host.lower()
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and not force and cached[1] > now:
            return cached[0]

    try:
        answer = _query_mdns_ipv4(host)
    except Exception as exc:
        logging.debug("mDNS resolve failed for %s: %s", host, exc)
        answer = None

    if answer:
        ip, ttl = answer
        expires = now + max(10, min(ttl, _CACHE_TTL_SECONDS))
        with _cache_lock:
            previous = _cache.get(key)
            _cache[key] = (ip, expires)
        _save_persistent_cache(key, ip)
        if not previous or previous[0] != ip:
            logging.info("mDNS resolved %s -> %s", host, ip)
        return ip

    with _cache_lock:
        cached = _cache.get(key)
    if cached:
        logging.debug("Using stale in-memory mDNS cache for %s -> %s", host, cached[0])
        return cached[0]

    cached_ip = _load_persistent_cache(key)
    if cached_ip:
        logging.warning("mDNS resolution failed for %s; using persisted cache %s", host, cached_ip)
        with _cache_lock:
            _cache[key] = (cached_ip, now + 10)
        return cached_ip

    logging.warning("mDNS resolution failed for %s; using hostname directly", host)
    return hostname


def url_with_host(url: str, host: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url

    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"

    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def resolve_camera_url(camera_url: str, *, force: bool = False) -> str:
    parsed = urlsplit(camera_url)
    if not parsed.hostname:
        return camera_url
    resolved_host = resolve_host(parsed.hostname, force=force)
    if resolved_host == parsed.hostname:
        return camera_url
    return url_with_host(camera_url, resolved_host)
