"""
llm_provider.py
---------------
LLM provider abstraction for NL2DB.

Priority order:
1. User-supplied API key (Groq or Gemini) — used first, no token counting
2. Hosted fallback key (your Groq/Gemini key) — rate-limited by token budget

Supported providers: groq, gemini
"""

import logging
import threading
from datetime import datetime, date

from config import cfg

logger = logging.getLogger(__name__)

# ─── Token budget for hosted fallback ────────────────────────────────────────
# Adjust these to match your free-tier limits
HOSTED_DAILY_TOKEN_BUDGET = cfg.HOSTED_DAILY_TOKEN_BUDGET

_budget_lock = threading.Lock()
_budget_state = {
    "date": date.today(),
    "tokens_used": 0,
}


def _check_and_consume_budget(estimated_tokens: int = 1000) -> bool:
    """Returns True if budget is available and consumes it. Thread-safe."""
    with _budget_lock:
        today = date.today()
        if _budget_state["date"] != today:
            # Reset daily budget
            _budget_state["date"] = today
            _budget_state["tokens_used"] = 0

        if _budget_state["tokens_used"] + estimated_tokens > HOSTED_DAILY_TOKEN_BUDGET:
            return False

        _budget_state["tokens_used"] += estimated_tokens
        return True


def get_budget_status() -> dict:
    """Returns current token budget status for the hosted key."""
    with _budget_lock:
        today = date.today()
        if _budget_state["date"] != today:
            return {"date": str(today), "tokens_used": 0, "budget": HOSTED_DAILY_TOKEN_BUDGET, "remaining": HOSTED_DAILY_TOKEN_BUDGET}
        return {
            "date": str(_budget_state["date"]),
            "tokens_used": _budget_state["tokens_used"],
            "budget": HOSTED_DAILY_TOKEN_BUDGET,
            "remaining": max(0, HOSTED_DAILY_TOKEN_BUDGET - _budget_state["tokens_used"]),
        }


# ─── Provider builder ─────────────────────────────────────────────────────────

def _build_groq_llm(api_key: str, model: str = None):
    """Build a LangChain-compatible Groq LLM."""
    try:
        from langchain_groq import ChatGroq
    except ImportError:
        raise ImportError("Install langchain-groq: pip install langchain-groq")

    model = model or "llama-3.3-70b-versatile"
    return ChatGroq(
        temperature=0,
        model_name=model,
        api_key=api_key,
    )


def _build_gemini_llm(api_key: str, model: str = None):
    """Build a LangChain-compatible Gemini LLM."""
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError:
        raise ImportError("Install langchain-google-genai: pip install langchain-google-genai")

    model = model or "gemini-1.5-flash"
    return ChatGoogleGenerativeAI(
        temperature=0,
        model=model,
        google_api_key=api_key,
    )


def _build_openai_llm(api_key: str, model: str = None):
    """Build a LangChain-compatible OpenAI LLM (legacy support)."""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        raise ImportError("Install langchain-openai: pip install langchain-openai")

    model = model or "gpt-4o-mini"
    return ChatOpenAI(
        temperature=0,
        model=model,
        api_key=api_key,
    )


_PROVIDER_BUILDERS = {
    "groq": _build_groq_llm,
    "gemini": _build_gemini_llm,
    "openai": _build_openai_llm,
}


def get_llm(
    user_api_key: str = None,
    user_provider: str = None,
    user_model: str = None,
    estimated_tokens: int = 1000,
):
    """
    Returns a LangChain LLM instance.

    Flow:
    1. If user_api_key is provided → use it with user_provider (default: groq)
    2. Else → use hosted fallback key (GROQ or GEMINI) with token budget check

    Args:
        user_api_key:    API key supplied by the end user
        user_provider:   "groq" | "gemini" | "openai"
        user_model:      Optional model override
        estimated_tokens: Rough token estimate for budget tracking (hosted only)

    Returns:
        A LangChain chat model instance

    Raises:
        RuntimeError: If no API key is available or budget exhausted
    """
    # ── Path 1: user-supplied key ──────────────────────────────────────────
    if user_api_key:
        provider = (user_provider or "groq").lower()
        builder = _PROVIDER_BUILDERS.get(provider)
        if not builder:
            raise ValueError(f"Unknown provider '{provider}'. Choose from: {list(_PROVIDER_BUILDERS.keys())}")
        logger.info(f"[LLM] Using user-supplied {provider} key")
        return builder(api_key=user_api_key, model=user_model)

    # ── Path 2: hosted fallback ────────────────────────────────────────────
    hosted_provider = cfg.HOSTED_LLM_PROVIDER.lower()
    hosted_key = cfg.HOSTED_LLM_API_KEY

    if not hosted_key:
        raise RuntimeError(
            "No API key available. Please provide your own API key in the settings, "
            "or set HOSTED_LLM_API_KEY in the server environment."
        )

    if not _check_and_consume_budget(estimated_tokens):
        budget = get_budget_status()
        raise RuntimeError(
            f"The free hosted service has reached its daily limit "
            f"({budget['tokens_used']}/{budget['budget']} tokens used). "
            f"Please provide your own Groq or Gemini API key to continue — it's free at console.groq.com or aistudio.google.com."
        )

    builder = _PROVIDER_BUILDERS.get(hosted_provider)
    if not builder:
        raise ValueError(f"Unknown hosted provider '{hosted_provider}'")

    logger.info(f"[LLM] Using hosted {hosted_provider} key (budget: {get_budget_status()['tokens_used']}/{HOSTED_DAILY_TOKEN_BUDGET})")
    return builder(api_key=hosted_key, model=user_model)


# ─── Session-scoped LLM cache ─────────────────────────────────────────────────
# Avoids rebuilding the LLM object on every request for the same session config.

_llm_cache: dict = {}
_llm_cache_lock = threading.Lock()


def get_llm_for_session(session_config: dict) -> object:
    """
    Returns a cached LLM for a given session config dict.

    session_config keys:
        api_key  (str|None)
        provider (str|None)  — "groq" | "gemini" | "openai"
        model    (str|None)
    """
    cache_key = (
        session_config.get("api_key"),
        session_config.get("provider", "groq"),
        session_config.get("model"),
    )

    with _llm_cache_lock:
        if cache_key not in _llm_cache:
            _llm_cache[cache_key] = get_llm(
                user_api_key=session_config.get("api_key"),
                user_provider=session_config.get("provider"),
                user_model=session_config.get("model"),
            )
        return _llm_cache[cache_key]
