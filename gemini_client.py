"""Shared Gemini REST client.

Lives in its own module so every AI feature uses one implementation of the
model-fallback and retry logic. Previously only ``bitlink_manager.py`` could
talk to Gemini, which meant the sales-insights feature in ``main_whatsapp.py``
had no way to reach the API without duplicating all of this.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request

# Real model names only. Unknown or retired names 404 and fall through.
CANDIDATE_MODELS = [
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-flash-lite",
]

# Only a genuine 404 (model/version unavailable) should advance to the next
# candidate. Everything else is worth retrying on the same model.
_TRANSIENT_CODES = (408, 429, 500, 502, 503, 504)


def api_key():
    """Resolve the key lazily so a .env loaded after import is still picked up."""
    return (os.getenv("GEMINI_API_KEY") or "").strip()


def call_gemini_api(prompt: str, timeout: int = 40, max_retries: int = 4) -> str:
    """Call Gemini, retrying transient failures before falling back.

    Gemini intermittently answers 503 "high demand". The previous version moved
    straight to the next model on any error, so a single blip exhausted the
    list and surfaced whichever 404 came last -- hiding the real cause and
    dropping the rep into a canned message. Transient errors are now retried on
    the same model, and only a genuine 404 advances to the next one.
    """
    key = api_key()
    if not key:
        raise ValueError("GEMINI_API_KEY is not configured")

    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    last_err = None
    last_transient = None

    for model_name in CANDIDATE_MODELS:
        for ver in ["v1beta", "v1"]:
            url = (
                f"https://generativelanguage.googleapis.com/{ver}/models/"
                f"{model_name}:generateContent?key={key}"
            )
            for attempt in range(max_retries):
                req = urllib.request.Request(url, data=payload, headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        if resp.status == 200:
                            data_json = json.loads(resp.read().decode("utf-8"))
                            candidates = data_json.get("candidates", [])
                            if candidates:
                                parts = candidates[0].get("content", {}).get("parts", [])
                                if parts and "text" in parts[0]:
                                    text = parts[0]["text"].strip()
                                    if text:
                                        logging.info(
                                            f"Gemini generation succeeded with model: "
                                            f"{model_name} ({ver})"
                                        )
                                        return text
                    last_err = RuntimeError(f"{model_name} ({ver}) returned no text")
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        # Model/version not available -- do not retry, advance.
                        last_err = e
                        break
                    if e.code in _TRANSIENT_CODES:
                        # Capacity/rate limit: worth waiting this model out.
                        last_transient = e
                        retry_after = e.headers.get("Retry-After") if e.headers else None
                        try:
                            wait = float(retry_after) if retry_after else 1.5 * (attempt + 1)
                        except (TypeError, ValueError):
                            wait = 1.5 * (attempt + 1)
                        wait = min(wait, 20.0)
                        logging.warning(
                            f"Gemini {e.code} from {model_name} ({ver}), "
                            f"retry {attempt + 1}/{max_retries} in {wait:.1f}s"
                        )
                        time.sleep(wait)
                        continue
                    last_err = e
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(0.5)
                    continue
            else:
                # Retries exhausted on this model/version: try the next one.
                continue

    if last_transient:
        raise last_transient
    if last_err:
        raise last_err
    return ""
