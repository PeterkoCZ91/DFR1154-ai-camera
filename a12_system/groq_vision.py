"""Groq vision-based face recognition for person whitelisting and Nuki unlock."""

import base64
import logging
import os
import time
from pathlib import Path

import cv2
import numpy as np
import requests


GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

# Thresholds
CONFIDENCE_AUTO = 0.90   # >= this → auto-unlock
CONFIDENCE_ASK  = 0.65   # >= this → Telegram confirm button
                          # <  this → unknown person

GROQ_COOLDOWN = 30.0     # seconds between API calls


def _encode_image(img: np.ndarray) -> str:
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return base64.b64encode(buf).decode("utf-8")


def _encode_file(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


class GroqVision:
    def __init__(self, api_key: str, known_faces_dir: str):
        self.api_key = api_key
        self.known_faces_dir = Path(known_faces_dir)
        self._ref_cache: dict[str, list[str]] = {}  # name → [b64, ...]
        self._cache_mtime: dict[str, float] = {}
        self._last_call: float = 0.0
        self._load_references()

    def _load_references(self):
        if not self.known_faces_dir.exists():
            return
        for person_dir in self.known_faces_dir.iterdir():
            if not person_dir.is_dir():
                continue
            name = person_dir.name
            images = sorted(person_dir.glob("*.jpg")) + sorted(person_dir.glob("*.png"))
            if not images:
                continue
            mtime = max(p.stat().st_mtime for p in images)
            if self._cache_mtime.get(name) == mtime:
                continue
            self._ref_cache[name] = [_encode_file(str(p)) for p in images[:3]]
            self._cache_mtime[name] = mtime
            logging.info(f"[GroqVision] Loaded {len(self._ref_cache[name])} ref(s) for '{name}'")

    def identify(self, frame: np.ndarray) -> tuple[str | None, float, str]:
        """
        Compare frame against all known faces.
        Returns (name, confidence, decision) where decision is:
          'auto'    — high confidence, safe to unlock automatically
          'confirm' — medium confidence, ask via Telegram
          'unknown' — not recognized
        """
        self._load_references()

        if not self._ref_cache:
            return None, 0.0, "unknown"

        now = time.time()
        if now - self._last_call < GROQ_COOLDOWN:
            return None, 0.0, "cooldown"
        self._last_call = now

        frame_b64 = _encode_image(frame)
        best_name, best_conf = None, 0.0

        for name, refs in self._ref_cache.items():
            conf = self._compare(frame_b64, refs, name)
            logging.info(f"[GroqVision] '{name}' confidence: {conf:.2f}")
            if conf > best_conf:
                best_conf = conf
                best_name = name

        if best_conf >= CONFIDENCE_AUTO:
            return best_name, best_conf, "auto"
        elif best_conf >= CONFIDENCE_ASK:
            return best_name, best_conf, "confirm"
        else:
            return None, best_conf, "unknown"

    def _compare(self, frame_b64: str, ref_b64_list: list[str], name: str) -> float:
        ref_content = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{r}"},
            }
            for r in ref_b64_list
        ]

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"These are reference photos of a person named '{name}'. "
                            "Is the person in the LAST image the same individual? "
                            "Consider face features, ignore clothing/lighting differences. "
                            "Reply with ONLY a JSON object: "
                            '{"match": true/false, "confidence": 0.0-1.0, "reason": "one sentence"}'
                        ),
                    },
                    *ref_content,
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"},
                    },
                ],
            }
        ]

        try:
            resp = requests.post(
                GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": messages,
                    "max_tokens": 100,
                    "temperature": 0.1,
                },
                timeout=10,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()

            import json, re
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if not m:
                logging.warning(f"[GroqVision] Unexpected response: {text}")
                return 0.0
            data = json.loads(m.group())
            if not data.get("match", False):
                return 0.0
            return float(data.get("confidence", 0.0))

        except Exception as e:
            logging.warning(f"[GroqVision] API error for '{name}': {e}")
            return 0.0
