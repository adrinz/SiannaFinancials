"""LLM enrichment layer - narrative polish, daily brief, ticket Q&A.

Design principles
-----------------
1. **Fail-open**: every public function returns ``None`` when a provider key
   is missing or a call errors. Callers treat deterministic output as ground
   truth and LLM responses as optional enrichment.
2. **Facts pinned**: prompts wrap structured values in XML-like tags and
   instruct the model to avoid introducing numbers not present in facts.
3. **Deterministic cache keys**: response cache is keyed by provider, model,
   task, and digest so identical requests do not re-bill.
4. **No math delegation**: the model never computes prices/Greeks/levels.
   Those values are generated in Python and passed to prompts as facts.

Environment
-----------
- ``SQUARE18_LLM_PROVIDER`` - ``auto`` (default), ``gemini``, ``anthropic``
- ``GEMINI_API_KEY`` or ``GOOGLE_API_KEY`` (Gemini provider key)
- ``GEMINI_MODEL`` (default ``gemini-2.5-flash``)
- ``ANTHROPIC_API_KEY`` (Anthropic provider key)
- ``ANTHROPIC_MODEL`` (default ``claude-sonnet-4-5``)
- ``SQUARE18_LLM_CACHE_TTL`` seconds (default 86400 = 1 day)
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_DEFAULT_PROVIDER = "auto"
_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
_DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"
_CACHE_DIR = Path.home() / ".cache" / "square18_signals" / "llm"
_CACHE_TTL = int(os.environ.get("SQUARE18_LLM_CACHE_TTL", 86400))  # 1 day
_MAX_QUESTION_LEN = 800  # reject absurdly long explain-prompts
_LLM_REQUEST_TIMEOUT_SEC = float(os.environ.get("SQUARE18_LLM_TIMEOUT_SEC", "8"))


def _run_with_timeout(fn, timeout_sec: float):
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn)
    try:
        return fut.result(timeout=timeout_sec)
    except (concurrent.futures.TimeoutError, Exception):
        fut.cancel()
        raise
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


class LLMUnavailable(RuntimeError):
    """Raised when a feature is requested but the API key is not set."""


# Last error message from an LLM call, surfaced by the API so the UI can
# show something useful (e.g. "insufficient credits") instead of a generic
# "call failed". Cleared on every successful call.
_LAST_ERROR: Optional[str] = None


def last_error() -> Optional[str]:
    return _LAST_ERROR


@dataclass(frozen=True)
class LLMConfig:
    enabled: bool
    provider: str
    model: str
    cache_dir: str


def _gemini_api_key() -> str:
    return os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip()


def _resolve_provider() -> str:
    raw = os.environ.get("SQUARE18_LLM_PROVIDER", _DEFAULT_PROVIDER).strip().lower()
    if raw not in {"auto", "gemini", "anthropic"}:
        raw = "auto"
    if raw == "gemini":
        return "gemini"
    if raw == "anthropic":
        return "anthropic"
    # auto mode: prefer Gemini (free tier), then Anthropic if configured.
    if _gemini_api_key():
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "gemini"


def config() -> LLMConfig:
    provider = _resolve_provider()
    if provider == "anthropic":
        enabled = bool(os.environ.get("ANTHROPIC_API_KEY"))
        model = os.environ.get("ANTHROPIC_MODEL", _DEFAULT_ANTHROPIC_MODEL)
    else:
        enabled = bool(_gemini_api_key())
        model = os.environ.get("GEMINI_MODEL", _DEFAULT_GEMINI_MODEL)
    return LLMConfig(
        enabled=enabled,
        provider=provider,
        model=model,
        cache_dir=str(_CACHE_DIR),
    )


# ---------------------------------------------------------------------------
# Internal — client, cache, call helper
# ---------------------------------------------------------------------------


def _get_anthropic_client():
    """Return a singleton Anthropic client, or None if disabled."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        from anthropic import Anthropic  # type: ignore
    except Exception:
        return None
    global _ANTHROPIC_CLIENT  # noqa: PLW0603
    if "_ANTHROPIC_CLIENT" not in globals():
        _ANTHROPIC_CLIENT = Anthropic()
    return _ANTHROPIC_CLIENT  # type: ignore[name-defined]


def _cache_path(task: str, model: str, digest: str) -> Path:
    return _CACHE_DIR / f"{task}__{model}__{digest}.json"


def _read_cache(task: str, model: str, digest: str) -> Optional[str]:
    p = _cache_path(task, model, digest)
    if not p.exists():
        return None
    try:
        age = time.time() - p.stat().st_mtime
        if age > _CACHE_TTL:
            return None
        payload = json.loads(p.read_text())
        return payload.get("text")
    except Exception:
        return None


def _write_cache(task: str, model: str, digest: str, text: str) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(task, model, digest).write_text(
            json.dumps({"text": text, "task": task, "model": model, "ts": time.time()})
        )
    except Exception:
        pass


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def _anthropic_call(
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
) -> Optional[str]:
    client = _get_anthropic_client()
    if client is None:
        return None

    def _do_call():
        return client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )

    msg = _run_with_timeout(_do_call, _LLM_REQUEST_TIMEOUT_SEC)
    parts = [
        b.text for b in msg.content
        if getattr(b, "type", None) == "text" and getattr(b, "text", None)
    ]
    text = "\n".join(parts).strip()
    return text or None


def _gemini_call(
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
) -> Optional[str]:
    api_key = _gemini_api_key()
    if not api_key:
        return None

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": float(temperature),
            "maxOutputTokens": int(max_tokens),
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(api_key)}"
    )
    req = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def _do_call() -> str:
        with urllib.request.urlopen(req, timeout=_LLM_REQUEST_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        candidates = data.get("candidates") or []
        if not candidates:
            return ""
        parts = (candidates[0].get("content") or {}).get("parts") or []
        texts = [str(p.get("text", "")).strip() for p in parts if p.get("text")]
        return "\n".join(t for t in texts if t).strip()

    text = _run_with_timeout(_do_call, _LLM_REQUEST_TIMEOUT_SEC)
    return text or None


def _provider_call(
    *,
    cfg: LLMConfig,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
) -> Optional[str]:
    if cfg.provider == "anthropic":
        return _anthropic_call(
            model=cfg.model,
            system=system,
            user=user,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    return _gemini_call(
        model=cfg.model,
        system=system,
        user=user,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def _call(
    *,
    task: str,
    system: str,
    user: str,
    cache_key: Any,
    max_tokens: int,
    temperature: float = 0.2,
    bypass_cache: bool = False,
) -> Optional[str]:
    """Internal: cached, fail-open call to configured provider."""
    cfg = config()
    if not cfg.enabled:
        return None

    digest = _digest(cache_key)
    cache_model = f"{cfg.provider}__{cfg.model}"
    if not bypass_cache:
        cached = _read_cache(task, cache_model, digest)
        if cached is not None:
            return cached

    global _LAST_ERROR  # noqa: PLW0603
    try:
        text = _provider_call(
            cfg=cfg,
            system=system,
            user=user,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if text:
            _write_cache(task, cache_model, digest, text)
            _LAST_ERROR = None
        return text or None
    except Exception as e:
        if isinstance(e, concurrent.futures.TimeoutError):
            _LAST_ERROR = f"{cfg.provider.capitalize()} request timed out"
            return None
        _LAST_ERROR = _summarize_provider_error(cfg.provider, e)
        return None


def _summarize_provider_error(provider: str, exc: BaseException) -> str:
    """Boil provider-specific errors down to one short, user-safe line."""
    name = type(exc).__name__
    msg = str(exc)

    if isinstance(exc, urllib.error.HTTPError):
        try:
            body_raw = exc.read().decode("utf-8", errors="replace")
            payload = json.loads(body_raw)
            if isinstance(payload, dict):
                inner = payload.get("error") or {}
                if isinstance(inner, dict):
                    msg = str(inner.get("message") or msg)
        except Exception:
            pass
        if exc.code == 401:
            if provider == "gemini":
                return "Gemini API key rejected (invalid or revoked)."
            return "Anthropic API key rejected (invalid or revoked)."
        if exc.code == 429:
            return f"{provider.capitalize()} rate-limited the request - try again shortly."

    try:
        body = getattr(exc, "body", None) or getattr(exc, "response", None)
        if body is not None:
            data = body if isinstance(body, dict) else getattr(body, "json", lambda: {})()
            inner = (data or {}).get("error") or {}
            detailed = inner.get("message")
            if detailed:
                msg = detailed
    except Exception:
        pass
    lower = msg.lower()
    if "resource exhausted" in lower or "quota" in lower:
        if provider == "gemini":
            return "Gemini free-tier quota exhausted - wait for reset or upgrade."
        return "Anthropic quota exhausted - check account credits."
    if "credit balance" in lower or "insufficient" in lower:
        if provider == "gemini":
            return "Gemini quota exhausted - wait for reset or upgrade."
        return "Anthropic account has no credits - add billing at console.anthropic.com."
    if "invalid api key" in lower or "authentication" in lower or "401" in lower:
        if provider == "gemini":
            return "Gemini API key rejected (invalid or revoked)."
        return "Anthropic API key rejected (invalid or revoked)."
    if "rate" in lower and "limit" in lower:
        return f"{provider.capitalize()} rate-limited the request - try again shortly."
    if "model" in lower and ("not found" in lower or "does not exist" in lower):
        return f"{provider.capitalize()} model not available to this account."
    if "timeout" in lower or "timed out" in lower:
        return f"{provider.capitalize()} request timed out - try again."
    # Fallback: first 160 chars of the message with the class name.
    return f"{name}: {msg[:160]}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_POLISH_SYSTEM = """\
You are a disciplined technical options analyst at a trading desk.

Non-negotiable rules:
1. Only use numbers, prices, levels, and indicator values that appear \
inside the <facts> tag. Do not introduce any price, percentage, or metric \
that is not explicitly in <facts>.
2. Do not invent news, earnings dates, catalysts, analyst actions, or \
macro events. You have no access to external information.
3. The verdict, composite score, contract type (call/put), strike, and \
expiry inside <facts> are ground truth. Never contradict them.
4. Output plain paragraphs only — no bullet lists, no headers, no \
markdown. 3 to 4 paragraphs, 350-450 words total.
5. Professional trader tone. No marketing language, no hype, no hedging \
filler ("it is important to note…"). No emojis.
6. If a piece of information is missing (e.g. SMA200 is None), omit it \
rather than speculate.

Structure the output as:
Paragraph 1 — price action, trend, and what structure implies for the \
recommended contract.
Paragraph 2 — indicator alignment (SMA stack, RSI, MACD, Stochastic %K/%D, \
ADX / +DI vs −DI, Bollinger band position, ATR) and volume confirmation/divergence.
Paragraph 3 — the specific trade: contract, strike, expiry, estimated \
cost, target, stop, and risk/reward from <facts>.
Paragraph 4 (optional, only if meaningful) — risks / thesis \
invalidation.
"""


def polish_narrative(report: dict) -> Optional[str]:
    """Rewrite the deterministic narrative as fluent analyst prose.

    ``report`` should be the ``ReportOut``-shaped dict returned by
    ``build_report``. Returns polished prose or ``None`` if disabled /
    failed.
    """
    # Strip the chart array — it adds hundreds of numbers that the model
    # shouldn't reason over at bar level (noise + tokens).
    lite = {k: v for k, v in report.items() if k != "chart"}
    user = (
        "<facts>\n"
        + json.dumps(lite, default=str, indent=2)
        + "\n</facts>\n\n"
        "Rewrite the analysis for a professional trader. Follow every rule "
        "in the system prompt. Do not add a disclaimer — the app already "
        "has one. Return only the 3-4 paragraphs of prose."
    )
    return _call(
        task="polish",
        system=_POLISH_SYSTEM,
        user=user,
        cache_key={"narrative": lite.get("narrative"), "headline": lite.get("headline"),
                   "options": lite.get("options", {}).get("headline"),
                   "sym": lite.get("symbol"), "tf": lite.get("timeframe")},
        max_tokens=900,
        temperature=0.3,
    )


_BRIEF_SYSTEM = """\
You are a disciplined buy-side market strategist writing a daily desk brief.

Non-negotiable rules:
1. Only reference tickers and numbers that appear inside <rows>.
2. No news, macro commentary, or catalysts that aren't derivable from the \
data.
3. Output must be markdown with the following structure and nothing else:

Opening: one line naming the bull/bear split and the strongest theme.
Then three sections with `###` headers: "Bullish setups", "Bearish \
setups", "Cross-ticker themes".
Each section is 2-4 short bullet points. Each bullet cites the symbol \
and one or two numbers (verdict, score, contract) from <rows>.

4. Total length under 250 words.
5. Do not predict future prices. Describe the current setup only.
6. If a sector has multiple tickers and they mostly agree, call it out \
(e.g. "3 of 3 nuclear names bearish"). Use the ``sector`` field.
"""


def market_brief(overview_rows: list[dict]) -> Optional[str]:
    """Synthesize today's setup across all tickers into a desk brief."""
    # Keep only the fields we want the model to reason over — drops noise.
    trimmed = [
        {
            "symbol": r.get("symbol"),
            "sector": r.get("sector"),
            "last": r.get("last"),
            "change_pct": r.get("change_pct"),
            "verdict": r.get("verdict"),
            "conviction": r.get("conviction"),
            "trend": r.get("trend"),
            "rsi": r.get("rsi"),
            "rec_contract_type": r.get("rec_contract_type"),
            "rec_strike": r.get("rec_strike"),
            "rec_risk_reward": r.get("rec_risk_reward"),
        }
        for r in overview_rows
    ]
    user = (
        "<rows>\n"
        + json.dumps(trimmed, default=str, indent=2)
        + "\n</rows>\n\n"
        "Write the desk brief. Follow every rule."
    )
    return _call(
        task="brief",
        system=_BRIEF_SYSTEM,
        user=user,
        cache_key=trimmed,
        max_tokens=600,
        temperature=0.3,
    )


_EXPLAIN_SYSTEM = """\
You are a senior options trader explaining a specific ticket to a \
junior analyst.

Non-negotiable rules:
1. Answer the question using only the data inside <report>. Cite numbers \
verbatim when relevant.
2. If the question asks about information not in <report> (IV smile, \
unusual options activity, specific earnings date, news), say clearly \
that the data isn't in this report rather than guessing.
3. 2-4 short paragraphs, under 250 words total. Plain prose, no \
markdown headers or lists.
4. Confident, plain language. No hedging filler.
5. Never recommend an alternative trade structure — the recommender in \
this app intentionally uses single-leg long calls / puts only. You may \
discuss strike or expiry trade-offs within that constraint.
"""


def explain_ticket(report: dict, question: str) -> Optional[str]:
    """Answer a user question against a specific report."""
    q = (question or "").strip()
    if not q:
        return None
    if len(q) > _MAX_QUESTION_LEN:
        q = q[:_MAX_QUESTION_LEN]

    lite = {k: v for k, v in report.items() if k != "chart"}
    user = (
        "<report>\n"
        + json.dumps(lite, default=str, indent=2)
        + "\n</report>\n\n"
        "<question>\n"
        + q
        + "\n</question>\n\n"
        "Answer following every rule."
    )
    return _call(
        task="explain",
        system=_EXPLAIN_SYSTEM,
        user=user,
        cache_key={"sym": lite.get("symbol"), "tf": lite.get("timeframe"),
                   "headline": lite.get("headline"),
                   "options": lite.get("options", {}).get("headline"),
                   "q": q},
        max_tokens=600,
        temperature=0.35,
    )
