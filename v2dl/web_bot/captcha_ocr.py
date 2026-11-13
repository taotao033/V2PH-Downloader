"""Client for the local HTTP captcha OCR service.

v2ph sometimes gates an album behind a 4-character image captcha. Instead
of running an ONNX model inside the downloader, the bots POST the
``#captcha-image`` PNG to a small local service (see the service README)
that keeps the model warm and can use a GPU. The contract this module
relies on:

* ``GET /health`` - liveness probe (``{"ok": true, "model": ..., ...}``);
* ``POST /ocr`` - one image; we use the JSON form
  ``{"image": "<base64>", "threshold": <float>}`` so a run can override
  the service default without restarting it.

The ``/ocr`` reply is ``{"ok": true, "text": "3bxr", "conf": 0.858,
"accepted": true, ...}``. ``conf`` is the minimum per-cell confidence and
``accepted`` is ``conf >= threshold`` - the documented gate callers should
trust. A captcha can always be refreshed, so the bots only submit an
answer when it is ``accepted`` and simply reload the image otherwise.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Final

# Default service address; matches ``python service/api.py``.
DEFAULT_BASE_URL: Final[str] = "http://127.0.0.1:8000"
# The service ships a 0.5 threshold (61% coverage at ~95% accuracy on its
# frozen validation set); config.yaml / env can raise it when a wrong
# answer costs more than another captcha reload.
DEFAULT_THRESHOLD: Final[float] = 0.5

ENV_BASE_URL: Final[str] = "V2DL_CAPTCHA_OCR_URL"
ENV_THRESHOLD: Final[str] = "V2DL_CAPTCHA_OCR_THRESHOLD"

# v2ph's captcha is always 4 ASCII alphanumeric characters. The bounds are
# deliberately loose: this guards against a misconfigured service, it does
# not second-guess a future model with a different cell count.
_MIN_TEXT_LEN: Final[int] = 3
_MAX_TEXT_LEN: Final[int] = 8


class CaptchaOcrError(RuntimeError):
    """The OCR service was unreachable or returned an unusable reply."""


@dataclass(frozen=True)
class CaptchaOcrResult:
    """One successful ``/ocr`` reply."""

    text: str
    conf: float
    accepted: bool
    cells: tuple[tuple[str, float], ...] = ()
    threshold: float | None = None
    model: str | None = None
    elapsed_ms: float | None = None


def resolve_base_url(configured: str | None = None) -> str:
    """Return the service base URL (env > config > default), no trailing slash."""
    raw = os.environ.get(ENV_BASE_URL) or configured or DEFAULT_BASE_URL
    return raw.strip().rstrip("/") or DEFAULT_BASE_URL


def resolve_threshold(configured: float | None = None) -> float:
    """Return the confidence threshold to request (env > config > default)."""
    raw = os.environ.get(ENV_THRESHOLD, "").strip()
    if raw:
        parsed = _as_float(raw)
        if parsed is not None and 0.0 <= parsed <= 1.0:
            return parsed
    if configured is not None:
        return _clamp_threshold(configured)
    return DEFAULT_THRESHOLD


def is_plausible_captcha_text(text: str) -> bool:
    """Return True when *text* could be a v2ph captcha answer.

    The service only emits the 31-character lowercase alphabet, so this is
    belt-and-braces validation in case the service URL points at something
    else (or the model behind it changes).
    """
    return (
        _MIN_TEXT_LEN <= len(text) <= _MAX_TEXT_LEN
        and text.isascii()
        and text.isalnum()
    )


def _clamp_threshold(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _error_detail(err: urllib.error.HTTPError) -> str:
    """Return the service's ``error`` message for an HTTP error, if any."""
    try:
        body = err.read()
        payload = json.loads(body.decode("utf-8"))
        if isinstance(payload, dict) and payload.get("error"):
            return str(payload["error"])
        return body.decode("utf-8", "replace").strip()[:200]
    except Exception:
        return str(err.reason or "")


class CaptchaOcrClient:
    """Minimal blocking client for the local captcha OCR HTTP service.

    Safe to reuse across captchas: the service serialises inference behind
    a lock, and the client keeps no per-request state besides the
    ``urllib`` opener (built with proxies disabled, because the service is
    always reached on the local machine / private network and a global
    ``HTTP_PROXY`` must not route 127.0.0.1 through a corporate proxy).
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        threshold: float | None = None,
        timeout: float = 30.0,
        health_timeout: float = 3.0,
    ) -> None:
        self.base_url = resolve_base_url(base_url)
        self.threshold = resolve_threshold(threshold)
        self.timeout = timeout
        self.health_timeout = health_timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def available(self) -> bool:
        """Return True when ``GET /health`` answers with ``ok: true``."""
        try:
            payload = self._request("GET", "/health", timeout=self.health_timeout)
        except CaptchaOcrError:
            return False
        return isinstance(payload, dict) and bool(payload.get("ok"))

    def recognize(
        self,
        img_bytes: bytes,
        *,
        threshold: float | None = None,
    ) -> CaptchaOcrResult:
        """Recognise one captcha image.

        Args:
            img_bytes: Raw PNG (or any image format the service decodes).
            threshold: One-shot confidence override for this request only.

        Raises:
            CaptchaOcrError: The service is unreachable, rejects the input,
                or answers with ``ok: false``.
        """
        if not img_bytes:
            raise CaptchaOcrError("captcha image is empty")

        limit = self.threshold if threshold is None else _clamp_threshold(threshold)
        payload = json.dumps(
            {
                "image": base64.b64encode(img_bytes).decode("ascii"),
                "threshold": limit,
            }
        ).encode("utf-8")

        reply = self._request(
            "POST",
            "/ocr",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        if not isinstance(reply, dict):
            raise CaptchaOcrError("OCR service returned a non-object JSON reply")
        if not reply.get("ok"):
            raise CaptchaOcrError(str(reply.get("error") or "OCR service returned ok=false"))

        text = reply.get("text")
        if not isinstance(text, str):
            raise CaptchaOcrError("OCR service reply has no text field")
        conf = _as_float(reply.get("conf"))
        if conf is None:
            raise CaptchaOcrError("OCR service reply has no numeric conf field")

        accepted_raw = reply.get("accepted")
        if isinstance(accepted_raw, bool):
            accepted = accepted_raw
        else:
            # Older / alternative service builds may omit ``accepted``;
            # fall back to the documented comparison against the
            # threshold the service reports for this reply.
            service_threshold = _as_float(reply.get("threshold"))
            accepted = conf >= (service_threshold if service_threshold is not None else limit)

        return CaptchaOcrResult(
            text=text,
            conf=conf,
            accepted=accepted,
            cells=_parse_cells(reply.get("cells")),
            threshold=_as_float(reply.get("threshold")),
            model=reply["model"] if isinstance(reply.get("model"), str) else None,
            elapsed_ms=_as_float(reply.get("ms")),
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send one request and return the decoded JSON body."""
        url = f"{self.base_url}{path}"
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)

        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:
            raise CaptchaOcrError(
                f"{method} {url} returned HTTP {err.code}: {_error_detail(err)}"
            ) from err
        except (urllib.error.URLError, OSError, ValueError) as err:
            # ``ValueError`` covers a malformed ``captcha_ocr_url`` (for
            # example a missing scheme), which ``Request`` rejects before
            # any I/O happens.
            reason = getattr(err, "reason", err)
            raise CaptchaOcrError(f"{method} {url} failed: {reason}") from err

        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise CaptchaOcrError(f"{method} {url} returned invalid JSON") from err


def _parse_cells(value: Any) -> tuple[tuple[str, float], ...]:
    if not isinstance(value, list):
        return ()
    cells: list[tuple[str, float]] = []
    for cell in value:
        if not isinstance(cell, dict):
            continue
        char = cell.get("char")
        conf = _as_float(cell.get("conf"))
        if isinstance(char, str) and conf is not None:
            cells.append((char, conf))
    return tuple(cells)
