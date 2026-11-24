"""Optional HTTP embedding client and vector helpers for the SQLite v2 Memory Engine.

Uses only the Python 3 standard library. Reads configuration live from
environment variables (`MEMORY_EMBED_URL`, `MEMORY_EMBED_MODEL`,
`MEMORY_EMBED_API_KEY`) with optional per-call overrides.

Hot-path safety:
- `embed_text()` enforces a strict timeout (default 0.8s) and NEVER raises
  unhandled exceptions; any timeout, network, HTTP, or parse error returns
  `None` so recall silently falls back to local 3-channel mode.
"""

from __future__ import annotations

import collections
import http.client
import json
import math
import operator
import os
import socket
import struct
import threading
import time
from typing import Any, Callable, Optional
import urllib.error
import urllib.parse
import urllib.request

ENV_EMBED_URL = "MEMORY_EMBED_URL"
ENV_EMBED_MODEL = "MEMORY_EMBED_MODEL"
ENV_EMBED_API_KEY = "MEMORY_EMBED_API_KEY"
ENV_EMBED_MIGRATION_BACKFILL_LIMIT = "MEMORY_EMBED_MIGRATION_BACKFILL_LIMIT"

DEFAULT_TIMEOUT = 0.8
DEFAULT_BACKFILL_TIMEOUT = 5.0
DEFAULT_BATCH_SIZE = 32
DEFAULT_MIGRATION_BACKFILL_LIMIT = 500
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MiB safety cap per embedding response
_FLOAT32_MAX = 3.4028234663852886e38

EMBED_CACHE_MAXSIZE = 256
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_OPEN_SECS = 60.0
_MAX_CIRCUIT_ENDPOINTS = 64

_CACHE_LOCK = threading.Lock()
_EMBED_CACHE: "collections.OrderedDict[tuple[str, str, str], tuple[float, ...]]" = (
    collections.OrderedDict()
)

_CB_LOCK = threading.Lock()
# target_url -> {"failures": int, "open_until": float, "half_open_probing": bool}
_CIRCUIT_STATE: dict[str, dict] = {}

__all__ = [
    "ENV_EMBED_URL",
    "ENV_EMBED_MODEL",
    "ENV_EMBED_API_KEY",
    "ENV_EMBED_MIGRATION_BACKFILL_LIMIT",
    "DEFAULT_TIMEOUT",
    "DEFAULT_BACKFILL_TIMEOUT",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MIGRATION_BACKFILL_LIMIT",
    "EMBED_CACHE_MAXSIZE",
    "CIRCUIT_FAILURE_THRESHOLD",
    "CIRCUIT_OPEN_SECS",
    "get_embed_config",
    "get_embed_fingerprint",
    "is_db_embedding_compatible",
    "is_enabled",
    "clear_cache",
    "reset_circuit_breaker",
    "pack_embedding",
    "unpack_embedding",
    "cosine_similarity",
    "embed_text",
    "embed_texts",
    "embed_memories_batch",
    "backfill_missing_embeddings",
]


def clear_cache() -> None:
    """Clear the in-memory embedding LRU query cache."""
    with _CACHE_LOCK:
        _EMBED_CACHE.clear()


def reset_circuit_breaker(url: str | None = None) -> None:
    """Reset failure circuit breaker state for one endpoint URL or all endpoints."""
    with _CB_LOCK:
        if url is None:
            _CIRCUIT_STATE.clear()
            return
        resolved = _resolve_endpoint(str(url).strip())
        key = resolved[0] if resolved else str(url).strip()
        _CIRCUIT_STATE.pop(key, None)


def _cache_get(target_url: str, model: str, text: str) -> list[float] | None:
    key = (target_url, model, text)
    with _CACHE_LOCK:
        vec = _EMBED_CACHE.get(key)
        if vec is None:
            return None
        _EMBED_CACHE.move_to_end(key)
        return list(vec)


def _cache_put(target_url: str, model: str, text: str, vec: list[float]) -> None:
    if not vec:
        return
    key = (target_url, model, text)
    stored = tuple(vec)
    with _CACHE_LOCK:
        _EMBED_CACHE[key] = stored
        _EMBED_CACHE.move_to_end(key)
        while len(_EMBED_CACHE) > EMBED_CACHE_MAXSIZE:
            _EMBED_CACHE.popitem(last=False)


def _circuit_allow_request(target_url: str) -> tuple[bool, bool]:
    """Return ``(allowed, is_half_open_probe)`` for ``target_url``."""
    now = time.monotonic()
    with _CB_LOCK:
        st = _CIRCUIT_STATE.get(target_url)
        if st is None:
            return True, False
        open_until = float(st.get("open_until", 0.0))
        if open_until <= 0.0:
            return True, False
        if now < open_until:
            return False, False
        # Cooldown elapsed -> allow a single half-open probe
        if st.get("half_open_probing"):
            return False, False
        st["half_open_probing"] = True
        return True, True


def _circuit_record_success(target_url: str) -> None:
    with _CB_LOCK:
        _CIRCUIT_STATE.pop(target_url, None)


def _circuit_release_probe(target_url: str) -> None:
    with _CB_LOCK:
        st = _CIRCUIT_STATE.get(target_url)
        if st is not None:
            st["half_open_probing"] = False


def _circuit_record_failure(target_url: str, was_half_open: bool = False) -> None:
    now = time.monotonic()
    with _CB_LOCK:
        st = _CIRCUIT_STATE.get(target_url)
        if st is None:
            if len(_CIRCUIT_STATE) >= _MAX_CIRCUIT_ENDPOINTS:
                oldest = next(iter(_CIRCUIT_STATE))
                _CIRCUIT_STATE.pop(oldest, None)
            st = {"failures": 0, "open_until": 0.0, "half_open_probing": False}
            _CIRCUIT_STATE[target_url] = st
        st["half_open_probing"] = False
        if was_half_open:
            st["failures"] = max(int(st.get("failures", 0)) + 1, CIRCUIT_FAILURE_THRESHOLD)
            st["open_until"] = now + CIRCUIT_OPEN_SECS
        else:
            failures = int(st.get("failures", 0)) + 1
            st["failures"] = failures
            if failures >= CIRCUIT_FAILURE_THRESHOLD:
                st["open_until"] = now + CIRCUIT_OPEN_SECS


def _db_module():
    if __package__:
        from . import db as _db
    else:
        from memory_engine import db as _db
    return _db


def get_embed_config(
    url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Read embedding settings from live environment with optional overrides."""
    resolved_url = (
        str(url).strip()
        if url is not None
        else os.environ.get(ENV_EMBED_URL, "").strip()
    )
    resolved_model = (
        str(model).strip()
        if model is not None
        else os.environ.get(ENV_EMBED_MODEL, "").strip()
    )
    resolved_api_key = (
        str(api_key).strip()
        if api_key is not None
        else os.environ.get(ENV_EMBED_API_KEY, "").strip()
    )
    resolved = _resolve_endpoint(resolved_url) if resolved_url else None
    target_url = resolved[0] if resolved is not None else ""
    style = resolved[1] if resolved is not None else ""
    return {
        "url": resolved_url,
        "target_url": target_url,
        "style": style,
        "model": resolved_model,
        "api_key": resolved_api_key,
        "enabled": bool(target_url),
    }


def _normalize_embed_cfg(cfg: dict | None = None) -> dict[str, Any]:
    """Normalize either a `get_embed_config()` dict or caller-supplied partial config."""
    if cfg is None:
        return get_embed_config()
    if not isinstance(cfg, dict):
        return {
            "url": "",
            "target_url": "",
            "style": "",
            "model": "",
            "api_key": "",
            "enabled": False,
        }
    raw_url = str(cfg.get("url") or "").strip()
    target_url = str(cfg.get("target_url") or "").strip()
    if not target_url and raw_url and cfg.get("enabled", True):
        resolved = _resolve_endpoint(raw_url)
        if resolved is not None:
            target_url = resolved[0]
    target_url = target_url.rstrip("/")
    if "enabled" in cfg:
        enabled = bool(cfg.get("enabled")) and bool(target_url)
    else:
        enabled = bool(target_url)
    return {
        "url": raw_url,
        "target_url": target_url,
        "style": str(cfg.get("style") or "").strip(),
        "model": str(cfg.get("model") or "").strip(),
        "api_key": str(cfg.get("api_key") or "").strip(),
        "enabled": enabled,
    }


def get_embed_fingerprint(cfg: dict | None = None) -> str:
    """Return canonical ``'{target_url}|{model}'`` fingerprint for active config, else ``''``."""
    norm = _normalize_embed_cfg(cfg)
    if not norm["enabled"] or not norm["target_url"]:
        return ""
    url = str(norm["target_url"]).strip().rstrip("/")
    model = str(norm.get("model") or "").strip()
    return f"{url}|{model}"


def is_db_embedding_compatible(conn, cfg: dict | None = None) -> bool:
    """Return True if stored DB embeddings are compatible with the active embedding config.

    Checks ``embedding_fingerprint`` first, falling back to legacy ``embedding_model``.
    Returns True for unstamped DBs (dimension checks guard legacy unstamped vectors).
    """
    norm = _normalize_embed_cfg(cfg)
    if not norm["enabled"]:
        return False
    db_mod = _db_module()
    stored_fp = db_mod.get_meta(conn, "embedding_fingerprint", "").strip()
    stored_model = db_mod.get_meta(conn, "embedding_model", "").strip()
    cur_fp = get_embed_fingerprint(norm)
    cur_model = str(norm.get("model") or "").strip()
    if stored_fp:
        return stored_fp == cur_fp
    if stored_model and cur_model:
        return stored_model == cur_model
    return True


def is_enabled(url: str | None = None) -> bool:
    """Return True iff the resolved embedding endpoint URL is a valid HTTP(S) URL."""
    return bool(get_embed_config(url=url)["enabled"])


def pack_embedding(vec: list[float]) -> bytes:
    """Pack a float vector into little-endian float32 bytes.

    Guards against NaN, Inf, non-numeric values, and float32 OverflowError;
    returns ``b""`` if invalid or empty.
    """
    if not vec:
        return b""
    cleaned: list[float] = []
    for item in vec:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return b""
        val = float(item)
        if not math.isfinite(val) or abs(val) > _FLOAT32_MAX:
            return b""
        cleaned.append(val)
    try:
        return struct.pack(f"<{len(cleaned)}f", *cleaned)
    except (OverflowError, struct.error, ValueError):
        return b""


def unpack_embedding(blob: bytes | memoryview | None) -> list[float]:
    """Unpack little-endian float32 bytes back into a list of floats."""
    if blob is None:
        return []
    if isinstance(blob, memoryview):
        raw = blob.tobytes()
    elif isinstance(blob, (bytes, bytearray)):
        raw = blob
    else:
        return []
    nbytes = len(raw)
    if nbytes == 0 or nbytes % 4 != 0:
        return []
    count = nbytes // 4
    return list(struct.unpack(f"<{count}f", raw))


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute cosine similarity in [-1.0, 1.0].

    Returns 0.0 on empty vectors, dimension mismatch, zero norm, or non-finite values.
    """
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    try:
        dot = sum(map(operator.mul, vec_a, vec_b))
        norm_a = sum(map(operator.mul, vec_a, vec_a))
        norm_b = sum(map(operator.mul, vec_b, vec_b))
    except TypeError:
        return 0.0
    if (
        norm_a <= 0.0
        or norm_b <= 0.0
        or not math.isfinite(dot)
        or not math.isfinite(norm_a)
        or not math.isfinite(norm_b)
    ):
        return 0.0
    denom = math.sqrt(norm_a) * math.sqrt(norm_b)
    if denom <= 0.0 or not math.isfinite(denom):
        return 0.0
    sim = dot / denom
    if not math.isfinite(sim):
        return 0.0
    if sim > 1.0:
        return 1.0
    if sim < -1.0:
        return -1.0
    return float(sim)


def _resolve_endpoint(raw_url: str) -> tuple[str, str] | None:
    """Normalize endpoint URL and detect protocol style.

    Returns `(target_url, style)` where style is one of:
    - `"ollama_legacy"` (path ends with `/api/embeddings`)
    - `"ollama_embed"` (path ends with `/api/embed`)
    - `"openai"` (OpenAI-compatible `/v1/embeddings` or custom embedding endpoint)

    Returns `None` for non-HTTP(S) or malformed URLs.
    """
    try:
        parsed = urllib.parse.urlsplit(raw_url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None

    norm_path = parsed.path.rstrip("/")
    lower_path = norm_path.lower()

    if lower_path.endswith("/api/embeddings"):
        target_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, norm_path, parsed.query, parsed.fragment)
        )
        return target_url, "ollama_legacy"
    if lower_path.endswith("/api/embed"):
        target_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, norm_path, parsed.query, parsed.fragment)
        )
        return target_url, "ollama_embed"
    if lower_path.endswith("/embeddings"):
        target_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, norm_path, parsed.query, parsed.fragment)
        )
        return target_url, "openai"

    if norm_path == "":
        new_path = "/v1/embeddings"
    else:
        new_path = norm_path + "/embeddings"
    target_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, new_path, parsed.query, parsed.fragment)
    )
    return target_url, "openai"


def _validate_vector(raw: object) -> list[float] | None:
    """Validate that `raw` is a non-empty sequence of finite float32-safe real numbers."""
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    vec: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        val = float(item)
        if not math.isfinite(val) or abs(val) > _FLOAT32_MAX:
            return None
        vec.append(val)
    return vec


def _extract_vector(data: object, style: str) -> list[float] | None:
    """Extract and validate a single embedding vector from a decoded JSON response."""
    if not isinstance(data, dict):
        return None

    candidate: object = None
    if style == "ollama_legacy":
        candidate = data.get("embedding")
        if candidate is None:
            embs = data.get("embeddings")
            if isinstance(embs, list) and embs:
                candidate = embs[0]
    elif style == "ollama_embed":
        embs = data.get("embeddings")
        if isinstance(embs, list) and embs:
            candidate = embs[0]
        if candidate is None:
            candidate = data.get("embedding")
    else:
        items = data.get("data")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            candidate = items[0].get("embedding")
        if candidate is None:
            candidate = data.get("embedding")
        if candidate is None:
            embs = data.get("embeddings")
            if isinstance(embs, list) and embs:
                candidate = embs[0]

    return _validate_vector(candidate)


def _extract_batch_vectors(
    data: object,
    style: str,
    expected_len: int,
) -> list[list[float]] | None:
    """Extract and validate a batch of exactly `expected_len` vectors, else return None."""
    if not isinstance(data, dict) or expected_len <= 0:
        return None

    if style == "ollama_embed":
        embs = data.get("embeddings")
        if not isinstance(embs, list) or len(embs) != expected_len:
            return None
        out: list[list[float]] = []
        for raw_vec in embs:
            v = _validate_vector(raw_vec)
            if v is None:
                return None
            out.append(v)
        return out

    # OpenAI-compatible: {"data": [{"index": 0, "embedding": [...]}, ...]}
    items = data.get("data")
    if isinstance(items, list) and len(items) == expected_len:
        indexed: list[list[float] | None] = [None] * expected_len
        use_index = all(
            isinstance(it, dict)
            and isinstance(it.get("index"), int)
            and not isinstance(it.get("index"), bool)
            and 0 <= it["index"] < expected_len
            for it in items
        )
        if use_index:
            for it in items:
                v = _validate_vector(it.get("embedding"))
                if v is None:
                    return None
                idx = int(it["index"])
                if indexed[idx] is not None:
                    return None
                indexed[idx] = v
            if all(v is not None for v in indexed):
                return [v for v in indexed if v is not None]
            return None

        out = []
        for it in items:
            if not isinstance(it, dict):
                return None
            v = _validate_vector(it.get("embedding"))
            if v is None:
                return None
            out.append(v)
        return out

    embs = data.get("embeddings")
    if isinstance(embs, list) and len(embs) == expected_len:
        out = []
        for raw_vec in embs:
            v = _validate_vector(raw_vec)
            if v is None:
                return None
            out.append(v)
        return out

    return None


def _post_json(
    target_url: str,
    payload: dict,
    api_key: str,
    timeout_val: float,
) -> tuple[object | None, str]:
    """Perform a bounded HTTP POST JSON request.

    Returns ``(decoded_json, error_kind)`` where ``error_kind`` is:
    - ``""`` on HTTP 2xx with valid JSON
    - ``"http_client_error"`` on HTTP 4xx (e.g. unsupported batch format)
    - ``"network_or_server"`` on HTTP 5xx, timeout, connection error, or parse failure
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            target_url,
            data=body,
            headers=headers,
            method="POST",
        )
        deadline = time.monotonic() + timeout_val
        with urllib.request.urlopen(req, timeout=timeout_val) as resp:
            status = getattr(resp, "status", None)
            if status is not None and not (200 <= int(status) < 300):
                return None, "network_or_server"
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return None, "network_or_server"
            raw_fp = getattr(getattr(resp, "fp", None), "raw", None)
            raw_sock = getattr(raw_fp, "_sock", None)
            if raw_sock is not None and hasattr(raw_sock, "settimeout"):
                raw_sock.settimeout(remaining)
            raw_body = resp.read(_MAX_RESPONSE_BYTES + 1)
        if not raw_body or len(raw_body) > _MAX_RESPONSE_BYTES:
            return None, "network_or_server"
        return json.loads(raw_body.decode("utf-8")), ""
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        if 400 <= code < 500:
            return None, "http_client_error"
        return None, "network_or_server"
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        socket.timeout,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ):
        return None, "network_or_server"
    except Exception:
        return None, "network_or_server"


def embed_text(
    text: str,
    timeout: float = DEFAULT_TIMEOUT,
    url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> list[float] | None:
    """Fetch an embedding vector for `text`, or return `None` on any error.

    Features a thread-safe LRU query cache (maxsize=256) and a per-endpoint
    failure circuit breaker (3 consecutive failures -> open for 60s before
    half-open retry) so a dead embedding endpoint never blocks hot-path recall.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        timeout_val = float(timeout)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(timeout_val) or timeout_val <= 0.0:
        return None

    cfg = get_embed_config(url=url, model=model, api_key=api_key)
    raw_url = cfg["url"]
    if not raw_url:
        return None

    resolved = _resolve_endpoint(raw_url)
    if resolved is None:
        return None
    target_url, style = resolved
    model_name = cfg["model"]

    cached = _cache_get(target_url, model_name, text)
    if cached is not None:
        return cached

    allowed, is_half_open = _circuit_allow_request(target_url)
    if not allowed:
        return None

    if style == "ollama_legacy":
        payload = {"model": model_name, "prompt": text}
    else:
        payload = {"model": model_name, "input": text}

    vec: list[float] | None = None
    try:
        data, err_kind = _post_json(target_url, payload, cfg["api_key"], timeout_val)
        if not err_kind and data is not None:
            vec = _extract_vector(data, style)
    finally:
        if vec is not None:
            _circuit_record_success(target_url)
            _cache_put(target_url, model_name, text, vec)
        else:
            _circuit_record_failure(target_url, was_half_open=is_half_open)

    return vec


def embed_texts(
    texts: list[str],
    timeout: float = DEFAULT_BACKFILL_TIMEOUT,
    url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[list[float] | None]:
    """Fetch embedding vectors for a batch of texts in chunks of up to 32.

    Falls back cleanly to single `embed_text()` calls if the endpoint rejects
    batch input or returns a mismatched vector count (e.g. single-item servers).
    Always returns a list matching `len(texts)`.
    """
    if not texts:
        return []
    results: list[list[float] | None] = [None] * len(texts)

    try:
        timeout_val = float(timeout)
    except (TypeError, ValueError):
        return results
    if not math.isfinite(timeout_val) or timeout_val <= 0.0:
        return results

    cfg = get_embed_config(url=url, model=model, api_key=api_key)
    raw_url = cfg["url"]
    if not raw_url:
        return results

    resolved = _resolve_endpoint(raw_url)
    if resolved is None:
        return results
    target_url, style = resolved
    model_name = cfg["model"]
    chunk_size = min(32, max(1, int(batch_size or DEFAULT_BATCH_SIZE)))

    # Map unique valid texts -> indices, filling LRU cache hits immediately
    unique_missing: list[str] = []
    text_to_indices: dict[str, list[int]] = {}
    for idx, item in enumerate(texts):
        if not isinstance(item, str) or not item.strip():
            continue
        cached = _cache_get(target_url, model_name, item)
        if cached is not None:
            results[idx] = cached
            continue
        if item not in text_to_indices:
            text_to_indices[item] = [idx]
            unique_missing.append(item)
        else:
            text_to_indices[item].append(idx)

    if not unique_missing:
        return results

    def _assign(text_key: str, vec: list[float] | None) -> None:
        if vec is None:
            return
        for pos in text_to_indices.get(text_key, ()):
            results[pos] = list(vec)

    # Ollama legacy /api/embeddings only supports single prompt payloads
    if style == "ollama_legacy":
        for txt in unique_missing:
            vec = embed_text(txt, timeout=timeout_val, url=url, model=model, api_key=api_key)
            _assign(txt, vec)
        return results

    batch_supported = True
    for start in range(0, len(unique_missing), chunk_size):
        chunk = unique_missing[start : start + chunk_size]
        if not batch_supported or len(chunk) == 1:
            for txt in chunk:
                vec = embed_text(
                    txt, timeout=timeout_val, url=url, model=model, api_key=api_key
                )
                _assign(txt, vec)
            continue

        allowed, is_half_open = _circuit_allow_request(target_url)
        if not allowed:
            break

        payload = {"model": model_name, "input": chunk}
        data = None
        err_kind = "network_or_server"
        try:
            data, err_kind = _post_json(target_url, payload, cfg["api_key"], timeout_val)
        except Exception:
            data, err_kind = None, "network_or_server"

        if not err_kind and data is not None:
            vecs = _extract_batch_vectors(data, style, expected_len=len(chunk))
            if vecs is not None and len(vecs) == len(chunk):
                _circuit_record_success(target_url)
                for txt, vec in zip(chunk, vecs):
                    _cache_put(target_url, model_name, txt, vec)
                    _assign(txt, vec)
                continue
            # Endpoint responded 200 but didn't return a matching batch (e.g. single-item server)
            _circuit_release_probe(target_url)
            batch_supported = False
            for txt in chunk:
                vec = embed_text(
                    txt, timeout=timeout_val, url=url, model=model, api_key=api_key
                )
                _assign(txt, vec)
            continue

        if err_kind == "http_client_error":
            # Server rejected batch input (4xx) -> fall back to single requests
            _circuit_release_probe(target_url)
            batch_supported = False
            for txt in chunk:
                vec = embed_text(
                    txt, timeout=timeout_val, url=url, model=model, api_key=api_key
                )
                _assign(txt, vec)
            continue

        # Network/timeout/5xx error -> record failure and fall back unless circuit opened
        _circuit_record_failure(target_url, was_half_open=is_half_open)
        for txt in chunk:
            vec = embed_text(
                txt, timeout=timeout_val, url=url, model=model, api_key=api_key
            )
            _assign(txt, vec)

    return results


def _blob_dim(blob: object) -> int:
    """Return float32 vector dimension from a packed embedding BLOB, or 0 if invalid."""
    if blob is None:
        return 0
    if isinstance(blob, memoryview):
        nbytes = blob.nbytes
    elif isinstance(blob, (bytes, bytearray)):
        nbytes = len(blob)
    else:
        return 0
    if nbytes <= 0 or nbytes % 4 != 0:
        return 0
    return nbytes // 4


def _read_stored_dim(conn) -> int:
    """Read ``embedding_dim`` from ``meta_kv``, falling back to 1 existing embedding BLOB."""
    db_mod = _db_module()
    raw = db_mod.get_meta(conn, "embedding_dim", "").strip()
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    row = conn.execute(
        "SELECT embedding FROM memories WHERE embedding IS NOT NULL LIMIT 1"
    ).fetchone()
    if row is not None:
        return _blob_dim(row[0])
    return 0


def embed_memories_batch(mem_ids: list[str], db_path: str | None = None) -> int:
    """Compute and store embeddings in batch for newly written active memory IDs.

    Refuses to write mixed-space vectors if the DB is bound to another embedding
    fingerprint/model or vector dimension. Never raises.
    """
    try:
        if not mem_ids:
            return 0
        cfg = get_embed_config()
        if not cfg["enabled"]:
            return 0

        clean_ids = list(
            dict.fromkeys(
                str(mid).strip() for mid in mem_ids if str(mid or "").strip()
            )
        )
        if not clean_ids:
            return 0

        db_mod = _db_module()
        cur_fp = get_embed_fingerprint(cfg)
        cur_model = str(cfg.get("model") or "").strip()

        rows: list[tuple[str, str]] = []
        stored_dim = 0
        read_ctx = db_mod.read_conn() if db_path is None else db_mod.read_conn(db_path)
        with read_ctx as conn:
            if not is_db_embedding_compatible(conn, cfg):
                return 0
            stored_dim = _read_stored_dim(conn)
            for start in range(0, len(clean_ids), 500):
                chunk_ids = clean_ids[start : start + 500]
                placeholders = ",".join("?" for _ in chunk_ids)
                fetched = conn.execute(
                    f"SELECT id, text FROM memories "
                    f"WHERE id IN ({placeholders}) "
                    "AND status = 'active' AND embedding IS NULL AND TRIM(text) != ''",
                    chunk_ids,
                ).fetchall()
                for row in fetched:
                    mid = str(row[0] or "").strip()
                    txt = row[1]
                    if mid and isinstance(txt, str) and txt.strip():
                        rows.append((mid, txt))

        if not rows:
            return 0

        vecs = embed_texts([t for _, t in rows], timeout=DEFAULT_BACKFILL_TIMEOUT)

        expected_dim = stored_dim
        updates: list[tuple[bytes, str, str, int]] = []
        for (mid, text), vec in zip(rows, vecs):
            valid = _validate_vector(vec)
            if not valid:
                continue
            vdim = len(valid)
            if expected_dim > 0 and vdim != expected_dim:
                continue
            if expected_dim <= 0:
                expected_dim = vdim
            blob = pack_embedding(valid)
            if blob:
                updates.append((blob, mid, text, vdim))

        if not updates:
            return 0

        updated = 0
        written_dim = 0
        write_ctx = db_mod.write_tx() if db_path is None else db_mod.write_tx(db_path)
        with write_ctx as conn:
            if not is_db_embedding_compatible(conn, cfg):
                return 0
            tx_dim = _read_stored_dim(conn)
            for blob, mid, text, vdim in updates:
                if tx_dim > 0 and vdim != tx_dim:
                    continue
                cur = conn.execute(
                    "UPDATE memories SET embedding = ? "
                    "WHERE id = ? AND text = ? AND status = 'active' AND embedding IS NULL",
                    (blob, mid, text),
                )
                if cur.rowcount and cur.rowcount > 0:
                    updated += int(cur.rowcount)
                    written_dim = vdim
                    if tx_dim <= 0:
                        tx_dim = vdim
            if updated > 0:
                if cur_model:
                    db_mod.set_meta(conn, "embedding_model", cur_model)
                if cur_fp:
                    db_mod.set_meta(conn, "embedding_fingerprint", cur_fp)
                if written_dim > 0:
                    db_mod.set_meta(conn, "embedding_dim", str(written_dim))

        return updated
    except Exception:
        return 0


def backfill_missing_embeddings(
    db_path: str | None = None,
    limit: int = 50,
    timeout: float = DEFAULT_BACKFILL_TIMEOUT,
    embed_fn: Optional[Callable[[str], list[float] | None]] = None,
    migration_limit: int | None = None,
) -> int:
    """Compute and store embeddings for active memories currently missing one.

    Tracks ``embedding_fingerprint``, ``embedding_model``, and ``embedding_dim`` in
    ``meta_kv``. On model/endpoint switches (or unstamped legacy embeddings), probes
    the configured endpoint FIRST before invalidating any stored embeddings so a dead
    or misconfigured endpoint can never wipe existing vectors. When a validated switch
    invalidates stale vectors, expands the backfill cap to
    ``MEMORY_EMBED_MIGRATION_BACKFILL_LIMIT`` (default 500) unless ``migration_limit=0``.
    """
    try:
        max_rows = int(limit)
    except (TypeError, ValueError):
        return 0
    if max_rows <= 0:
        return 0

    use_batch = False
    cfg: dict[str, Any] = {}
    cur_model = ""
    cur_fp = ""
    if embed_fn is None or embed_fn is embed_text:
        cfg = get_embed_config()
        if not cfg["enabled"]:
            return 0
        use_batch = True
        cur_model = str(cfg.get("model") or "").strip()
        cur_fp = get_embed_fingerprint(cfg)

    db_mod = _db_module()
    get_meta = db_mod.get_meta
    set_meta = db_mod.set_meta
    read_conn = db_mod.read_conn
    write_tx = db_mod.write_tx

    stored_fp = ""
    stored_model = ""
    stored_dim = 0
    has_existing_emb = False
    existing_dim = 0
    model_switched = False
    need_probe = False
    probe_text = ""

    read_ctx = read_conn() if db_path is None else read_conn(db_path)
    with read_ctx as conn:
        if use_batch:
            stored_fp = get_meta(conn, "embedding_fingerprint", "").strip()
            stored_model = get_meta(conn, "embedding_model", "").strip()
            stored_dim_raw = get_meta(conn, "embedding_dim", "").strip()
            if stored_dim_raw:
                try:
                    parsed_dim = int(stored_dim_raw)
                    if parsed_dim > 0:
                        stored_dim = parsed_dim
                except (TypeError, ValueError):
                    stored_dim = 0
            emb_row = conn.execute(
                "SELECT embedding FROM memories WHERE embedding IS NOT NULL LIMIT 1"
            ).fetchone()
            if emb_row is not None:
                has_existing_emb = True
                existing_dim = _blob_dim(emb_row[0])
                if stored_dim <= 0 and existing_dim > 0:
                    stored_dim = existing_dim

            if stored_fp and cur_fp and stored_fp != cur_fp:
                model_switched = True
            elif not stored_fp and stored_model and cur_model and stored_model != cur_model:
                model_switched = True

            need_probe = model_switched or (has_existing_emb and not stored_fp)
            if need_probe:
                p_row = conn.execute(
                    "SELECT text FROM memories "
                    "WHERE status = 'active' AND TRIM(text) != '' "
                    "ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                if p_row is not None and isinstance(p_row[0], str) and p_row[0].strip():
                    probe_text = p_row[0]
                else:
                    probe_text = "memory_embed_probe"

    # Probe BEFORE invalidating or stamping metadata on model/endpoint switches
    # or legacy unstamped DBs so a dead endpoint never destroys valid vectors.
    if use_batch and need_probe:
        probe_vecs = embed_texts([probe_text], timeout=timeout)
        probe_vec = _validate_vector(probe_vecs[0]) if probe_vecs else None
        if not probe_vec:
            return 0
        probe_dim = len(probe_vec)

        if not model_switched and existing_dim > 0 and probe_dim != existing_dim:
            model_switched = True

        write_ctx = write_tx() if db_path is None else write_tx(db_path)
        with write_ctx as conn:
            if model_switched:
                conn.execute(
                    "UPDATE memories SET embedding = NULL WHERE embedding IS NOT NULL"
                )
                if cur_model or stored_model:
                    set_meta(conn, "embedding_model", cur_model)
                if cur_fp:
                    set_meta(conn, "embedding_fingerprint", cur_fp)
                set_meta(conn, "embedding_dim", str(probe_dim))
            else:
                if cur_model:
                    set_meta(conn, "embedding_model", cur_model)
                if cur_fp:
                    set_meta(conn, "embedding_fingerprint", cur_fp)
                set_meta(conn, "embedding_dim", str(probe_dim))
        stored_fp = cur_fp
        stored_model = cur_model
        stored_dim = probe_dim

    effective_limit = max_rows
    if use_batch and model_switched:
        if migration_limit is not None:
            try:
                mig_cap = int(migration_limit)
            except (TypeError, ValueError):
                mig_cap = 0
        else:
            mig_cap = db_mod.get_env_int(
                ENV_EMBED_MIGRATION_BACKFILL_LIMIT,
                DEFAULT_MIGRATION_BACKFILL_LIMIT,
            )
        if mig_cap > 0:
            effective_limit = max(max_rows, mig_cap)

    read_ctx = read_conn() if db_path is None else read_conn(db_path)
    with read_ctx as conn:
        rows = conn.execute(
            "SELECT id, text FROM memories "
            "WHERE status = 'active' AND embedding IS NULL AND TRIM(text) != '' "
            "ORDER BY updated_at DESC "
            "LIMIT ?",
            (effective_limit,),
        ).fetchall()

    if not rows:
        return 0

    valid_items: list[tuple[object, str]] = []
    for row in rows:
        mem_id = row[0]
        mem_text = row[1]
        if isinstance(mem_text, str) and mem_text.strip():
            valid_items.append((mem_id, mem_text))

    if not valid_items:
        return 0

    if use_batch:
        vecs = embed_texts([t for _, t in valid_items], timeout=timeout)
    else:
        vecs = []
        assert embed_fn is not None
        for _, mem_text in valid_items:
            try:
                vecs.append(embed_fn(mem_text))
            except Exception:
                vecs.append(None)

    expected_dim = stored_dim if use_batch else 0
    updates: list[tuple[bytes, object, str, int]] = []
    for (mem_id, text), vec in zip(valid_items, vecs):
        valid = _validate_vector(vec)
        if not valid:
            continue
        vdim = len(valid)
        if use_batch:
            if expected_dim > 0 and vdim != expected_dim:
                continue
            if expected_dim <= 0:
                expected_dim = vdim
        blob = pack_embedding(valid)
        if blob:
            updates.append((blob, mem_id, text, vdim))

    if not updates:
        return 0

    updated = 0
    written_dim = 0
    write_ctx = write_tx() if db_path is None else write_tx(db_path)
    with write_ctx as conn:
        if use_batch and not is_db_embedding_compatible(conn, cfg):
            return 0
        tx_dim = _read_stored_dim(conn) if use_batch else 0
        for blob, mem_id, text, vdim in updates:
            if use_batch and tx_dim > 0 and vdim != tx_dim:
                continue
            cur = conn.execute(
                "UPDATE memories SET embedding = ? "
                "WHERE id = ? AND text = ? AND status = 'active' AND embedding IS NULL",
                (blob, mem_id, text),
            )
            if cur.rowcount and cur.rowcount > 0:
                updated += int(cur.rowcount)
                written_dim = vdim
                if use_batch and tx_dim <= 0:
                    tx_dim = vdim
        if use_batch and updated > 0:
            if cur_model:
                set_meta(conn, "embedding_model", cur_model)
            if cur_fp:
                set_meta(conn, "embedding_fingerprint", cur_fp)
            if written_dim > 0:
                set_meta(conn, "embedding_dim", str(written_dim))

    return updated
