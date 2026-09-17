"""Operator-visible message catalogue: English source text -> Czech.

Every string this system shows its operator on Telegram appears here once.
The key is the exact English text as it is written at the call site, so a
lookup is a plain dict hit; ``{}`` marks a value the caller interpolates,
in the same order in both languages.

The daily summary still ships its labels in Czech at the call site. Those
entries carry the English label the source is moving to as the key and the
current Czech literal as the value, so switching the source to English
loses nothing. Layout -- indentation, leading newlines -- stays in the
format strings and is not part of a key.
"""

import re

CZECH: dict[str, str] = {
    # pipeline.py — frame-health and stream-freeze watchdogs
    'Camera view after recovery:': 'Pohled kamery po obnově:',
    'Camera image has almost no detail and the exposure rewrite could not be applied — the camera is not answering on port 80. (rate-limited alert)': 'Obraz kamery je téměř bez detailu a přepis expozice se nepodařilo provést — kamera neodpovídá na portu 80. (opakování omezeno)',
    'Camera image has almost no detail. Rewriting the exposure registers (AEC/AGC) to clear a possible wedge — no reboot, no downtime. (rate-limited alert)': 'Obraz kamery je téměř bez detailu. Přepisuji registry expozice (AEC/AGC), aby se uvolnilo případné zatuhnutí — bez restartu a bez výpadku. (opakování omezeno)',
    'Camera image still has no detail after repeated exposure rewrites. The sensor is still producing new frames, so the readout has not stopped — what is left is the scene itself: a genuinely dark or featureless view, or a blocked lens.': 'Obraz kamery je bez detailu i po opakovaném přepisu expozice. Senzor dál posílá nové snímky, takže čtení se nezastavilo — zbývá samotná scéna: opravdu tmavý nebo prázdný záběr, nebo zakrytý objektiv.',
    'Camera is sending the same frame over and over — the sensor has stopped reading out (this is not darkness). Rebooting it over the LAN. (rate-limited alert)': 'Kamera posílá pořád tentýž snímek — senzor přestal číst obraz (není to tmou). Restartuji ji po síti. (opakování omezeno)',
    'Camera sensor is still repeating the same frame after {} reboots. A soft restart cannot clear it — the camera needs a physical power cycle.': 'Senzor kamery opakuje tentýž snímek i po {} restartech. Softwarový restart to nevyřeší — kamera potřebuje odpojit od napájení.',
    'Stream recovered — frames are healthy again.': 'Stream je obnovený — snímky jsou zase v pořádku.',
    'Stream stable again; sustained healthy frames confirmed.': 'Stream je zase stabilní; delší dobu chodí zdravé snímky.',
    'Camera stream keeps freezing — rebooting it over the LAN to recover. (rate-limited alert)': 'Stream z kamery se pořád zasekává — restartuji kameru po síti. (opakování omezeno)',
    'Camera keeps freezing even after repeated reboots — needs a manual look.': 'Kamera se zasekává i po opakovaných restartech — je potřeba se na ni podívat osobně.',

    # pipeline.py — detection clip captions
    '{} detected (AV Clip)': 'Detekce: {} (klip se zvukem)',
    '{} detected (Video)': 'Detekce: {} (video)',
    '{} detected': 'Detekce: {}',
    '{} - local MP4 saved': '{} – MP4 uloženo lokálně',

    # pipeline.py — Nuki unlock
    'Unlocked for {}': 'Odemknuto pro {}',

    # status_monitor.py — sabotage watchdog and health checks
    'SABOTAGE! Camera signal lost ({}s)!': 'SABOTÁŽ! Ztracen signál z kamery ({} s)!',
    'Camera stream lost ({}s) — reconnecting...': 'Ztracen stream z kamery ({} s) — připojuji znovu...',
    'Camera signal recovered.': 'Signál z kamery je zpět.',
    'ESP32 Health Warning: {}': 'Varování o stavu ESP32: {}',
    'ESP32 Power Warning: reason={} total_restarts={} poweron={} brownout={} uptime={}s': 'Varování o napájení ESP32: příčina={} restartů celkem={} zapnutí={} podpětí={} běží={}s',
    'A12 resource warning\nRAM: {}/{} MB ({}%)\nConsider reducing clip buffer or raising mem_limit in compose.': 'A12 varování o zdrojích\nRAM: {}/{} MB ({} %)\nZvaž zmenšení bufferu klipů nebo zvýšení mem_limit v compose.',

    # status_monitor.py — daily summary
    'A12 daily summary': 'A12 denní přehled',
    'Events (24h):': 'Události (24h):',
    'PIR/HA triggers': 'PIR/HA spuštění',
    'Local clips': 'Lokální klipy',
    'Confirmed person': 'Potvrzená osoba',
    'Dog': 'Pes',
    'Audio alerts': 'Audio alerty',
    'Stream interruptions': 'Přerušení streamu',
    '(none)': '(žádné)',
    'Scorer (since start): {} OK / {} errors / {} fallbacks, p95 {}s': 'Scorer (od startu): {} OK / {} chyb / {} fallbacků, p95 {}s',
    'Faces (24h): {} known / {} strangers / {} with no readable face': 'Obličeje (24h): {} známých / {} cizích / {} bez čitelného obličeje',

    # __main__.py — startup banner and stream availability
    'A12 System v2 started\nRuntime: {}\nMode: {}\nCamera: {} ({})\nCamera URL: {}\nMQTT base topic: {}\nKnown faces: {}\nLimits: {}\nData: {}': 'A12 System v2 nastartoval\nProstředí: {}\nRežim: {}\nKamera: {} ({})\nURL kamery: {}\nZákladní MQTT téma: {}\nZnámé obličeje: {}\nLimity: {}\nData: {}',
    '\nNote: low-detail image episode still active (low light is possible).': '\nPozn.: epizoda obrazu bez detailu stále trvá (může jít o málo světla).',
    'ESP32 is back online!': 'ESP32 je zase online!',
    'ESP32 not responding (STUCK)!': 'ESP32 neodpovídá (ZASEKNUTÉ)!',
}


def translate(text: str, language: str) -> str:
    """Render one operator-visible message in the chosen language.

    Anything not in the catalogue is returned as written. That is the important
    half: a new alert nobody has translated yet still reaches the operator in
    English, rather than disappearing or arriving empty. What keeps the gap
    from growing is test_message_catalogue.py, which fails when a call site
    passes a literal this file does not know.
    """
    if language == "cz":
        return CZECH.get(text, text)
    return text


def _templates():
    """Catalogue entries with placeholders, compiled to match a finished message.

    By the time the notifier sees an alert its values are already substituted:
    "Camera stream lost (42s)" is not the key "Camera stream lost ({}s)", so an
    exact lookup misses all 14 entries that interpolate anything. Each of those
    becomes a regex with a lazy capture per {}, and the Czech template is filled
    with whatever was captured.

    Longest literal text first, so a template cannot be stolen by a shorter one
    it contains: "{} detected" would otherwise claim "Person detected (Video)"
    and render the wrong sentence with no sign anything went wrong.
    """
    compiled = []
    for english, czech in CZECH.items():
        if "{}" not in english:
            continue
        pattern = "(.*?)".join(re.escape(part) for part in english.split("{}"))
        literal_length = len(english) - 2 * english.count("{}")
        compiled.append((literal_length, re.compile(f"^{pattern}$", re.S), czech))
    compiled.sort(key=lambda item: -item[0])
    return [(regex, czech) for _length, regex, czech in compiled]


_TEMPLATES = _templates()


def _render(text: str) -> str | None:
    """The Czech rendering of one message, or None if nothing matches."""
    if text in CZECH:
        return CZECH[text]
    for regex, czech in _TEMPLATES:
        match = regex.match(text)
        if match:
            return czech.format(*match.groups())
    return None


def translate_outgoing(text: str, language: str) -> str:
    """Translate a message that may carry the camera's label in front of it.

    ``_telegram_message()`` prepends "<camera name>: " before the notifier ever
    sees the string, so an exact lookup would miss every labelled alert. The
    label is not translatable — it is the operator's own name for the device.
    """
    if language != "cz" or not text:
        return text
    rendered = _render(text)
    if rendered is not None:
        return rendered
    prefix, separator, body = text.partition(": ")
    if separator:
        rendered = _render(body)
        if rendered is not None:
            return f"{prefix}: {rendered}"
    return text
