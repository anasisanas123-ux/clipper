"""Connected AI providers for virality scoring.

Claude goes through Anthropic's own SDK. Every other provider is reached through the OpenAI-compatible
Chat Completions API, which OpenAI, Gemini, Groq, DeepSeek, OpenRouter, xAI and local Ollama all offer."""
import json
import re
import time

import anthropic
import httpx

from . import config

PROVIDERS = {
    "anthropic": {"label": "Claude (Anthropic)", "base": None, "needs_key": True,
                  "key_url": "https://console.anthropic.com/settings/keys"},
    "openai": {"label": "OpenAI (ChatGPT)", "base": "https://api.openai.com/v1", "needs_key": True,
               "key_url": "https://platform.openai.com/api-keys"},
    "gemini": {"label": "Google Gemini", "base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "needs_key": True, "key_url": "https://aistudio.google.com/apikey"},
    "groq": {"label": "Groq", "base": "https://api.groq.com/openai/v1", "needs_key": True,
             "key_url": "https://console.groq.com/keys"},
    "deepseek": {"label": "DeepSeek", "base": "https://api.deepseek.com/v1", "needs_key": True,
                 "key_url": "https://platform.deepseek.com/api_keys"},
    "openrouter": {"label": "OpenRouter (many models, one key)", "base": "https://openrouter.ai/api/v1",
                   "needs_key": True, "key_url": "https://openrouter.ai/keys"},
    "xai": {"label": "xAI (Grok)", "base": "https://api.x.ai/v1", "needs_key": True,
            "key_url": "https://console.x.ai"},
    "ollama": {"label": "Ollama (runs on this PC, free)", "base": "http://localhost:11434/v1", "needs_key": False,
               "key_url": "https://ollama.com/download"},
    "custom": {"label": "Custom provider (OpenAI-compatible)", "base": None, "needs_key": False, "key_url": ""},
}
# USD per million tokens (input, output). Only Claude's are known here; other providers show token counts.
CLAUDE_PRICES = {
    "claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50), "claude-fable-5-2": (8.0, 40.0),
}


class AIError(RuntimeError):
    """A failure worth showing to the user as-is."""


def label(provider: str) -> str:
    return PROVIDERS.get(provider, {}).get("label", provider).split(" (")[0]


def base_url(settings: dict, provider: str) -> str:
    if provider == "custom":
        return (settings.get("custom_base_url") or "").strip().rstrip("/")
    return PROVIDERS[provider]["base"]


def readiness(settings: dict) -> tuple:
    """(ready, reason). Ready means jobs will be scored by the connected AI."""
    if settings.get("scorer") != "ai":
        return False, "Built-in algorithm selected"
    p = settings.get("ai_provider")
    if p not in PROVIDERS:
        return False, "No AI provider chosen"
    if PROVIDERS[p]["needs_key"] and not config.ai_key(settings, p):
        return False, f"No API key saved for {label(p)}"
    if p == "custom" and not base_url(settings, p):
        return False, "No address set for the custom provider"
    if not config.ai_model(settings, p):
        return False, f"No model chosen for {label(p)}"
    return True, f"{label(p)} · {config.ai_model(settings, p)}"


# ---------------------------------------------------------------- model lists

def list_models(settings: dict, provider: str, key: str = None) -> list:
    """Model ids the key can use, straight from the provider, so the list is never out of date."""
    key = key if key is not None else config.ai_key(settings, provider)
    if PROVIDERS[provider]["needs_key"] and not key:
        raise AIError(f"Enter your {label(provider)} API key first.")
    if provider == "anthropic":
        try:
            client = anthropic.Anthropic(api_key=key)
            return [m.id for m in client.models.list(limit=100)]
        except Exception as e:
            raise AIError(friendly(e, provider))
    base = base_url(settings, provider)
    if not base:
        raise AIError("Enter the provider's address (base URL) first.")
    try:
        r = httpx.get(base + "/models", headers=_headers(key, provider), timeout=30)
        _check(r, provider)
    except httpx.HTTPError as e:
        raise AIError(friendly(e, provider))
    data = r.json()
    ids = [m.get("id") or m.get("name") for m in data.get("data", data.get("models", []))]
    ids = [i.removeprefix("models/") for i in ids if i]
    # Drop models that can't chat (embeddings, speech, images, moderation).
    skip = re.compile(r"embed|whisper|tts|speech|audio|image|dall-e|moderation|transcri|realtime|veo|imagen|aqa",
                      re.I)
    return sorted({i for i in ids if not skip.search(i)})


# ---------------------------------------------------------------- calls

def ask_json(settings: dict, system: str, user: str, schema: dict) -> tuple:
    """Sends the prompt to the chosen provider and returns (parsed JSON, usage info)."""
    p = settings["ai_provider"]
    model = config.ai_model(settings, p)
    if p == "anthropic":
        text, info = _anthropic(settings, model, system, user, schema)
    else:
        text, info = _compatible(settings, p, model, system, user, schema)
    info["provider"] = label(p)
    return _parse(text), info


def _anthropic(settings, model, system, user, schema):
    client = anthropic.Anthropic(api_key=config.ai_key(settings, "anthropic"))
    kwargs = dict(model=model, max_tokens=16000, system=system, messages=[{"role": "user", "content": user}],
                  output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}})
    try:
        try:
            # Lets the API hand the request to another model if a safety classifier declines it.
            with client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                             **kwargs) as stream:
                resp = stream.get_final_message()
        except (TypeError, anthropic.BadRequestError):
            with client.messages.stream(**kwargs) as stream:
                resp = stream.get_final_message()
    except anthropic.APIError as e:
        raise AIError(friendly(e, "anthropic"))
    if resp.stop_reason == "refusal":
        raise AIError("Claude declined to analyse this transcript.")
    if resp.stop_reason == "max_tokens":
        raise AIError("Claude's answer was cut off.")
    pin, pout = CLAUDE_PRICES.get(model, (None, None))
    cost = None if pin is None else round((resp.usage.input_tokens * pin + resp.usage.output_tokens * pout) / 1e6, 3)
    text = next(b.text for b in resp.content if b.type == "text")
    return text, {"model": resp.model, "input_tokens": resp.usage.input_tokens,
                  "output_tokens": resp.usage.output_tokens, "cost_usd": cost}


def _headers(key, provider):
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    if provider == "openrouter":
        h["X-Title"] = "Clipper"
    return h


def _compatible(settings, provider, model, system, user, schema):
    base = base_url(settings, provider)
    key = config.ai_key(settings, provider)
    strict = {"type": "json_schema", "json_schema": {"name": "moments", "strict": True, "schema": schema}}
    spelled_out = (user + "\n\nReply with only a JSON object (no other text) that follows this JSON Schema:\n"
                   + json.dumps(schema))
    # Not every provider or model supports strict schemas; step down until one is accepted.
    attempts = [(strict, user), ({"type": "json_object"}, spelled_out), (None, spelled_out)]
    last = None
    for fmt, prompt in attempts:
        body = {"model": model, "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": prompt}]}
        if fmt:
            body["response_format"] = fmt
        r = _post_retrying(base + "/chat/completions", body, _headers(key, provider), provider)
        if r.status_code in (400, 422) and fmt and re.search(r"response_format|json|schema", r.text, re.I):
            last = r
            continue
        _check(r, provider)
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        if choice.get("finish_reason") == "length":
            raise AIError(f"{label(provider)}'s answer was cut off. Try a model with a larger context or output limit.")
        if not text.strip():
            raise AIError(f"{label(provider)} returned an empty answer.")
        usage = data.get("usage") or {}
        return text, {"model": data.get("model") or model, "input_tokens": usage.get("prompt_tokens"),
                      "output_tokens": usage.get("completion_tokens"), "cost_usd": None}
    _check(last, provider)


RETRY_WAITS = (8, 25, 60)   # seconds between attempts when the provider is busy or rate-limited


def _post_retrying(url, body, headers, provider):
    """POST that waits and tries again when the provider says it is overloaded (503, "high demand") or
    rate-limited (429), which is usually over within a minute. A used-up daily quota still fails at the end."""
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=httpx.Timeout(900, connect=20))
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
            if attempt == len(RETRY_WAITS):
                raise AIError(friendly(e, provider))
            time.sleep(RETRY_WAITS[attempt])
            continue
        except httpx.HTTPError as e:
            raise AIError(friendly(e, provider))
        if r.status_code not in (429, 500, 502, 503, 504) or attempt == len(RETRY_WAITS):
            return r
        try:
            wait = min(90.0, float(r.headers.get("retry-after", "")))
        except ValueError:
            wait = RETRY_WAITS[attempt]
        time.sleep(wait)
    return r


def _check(r, provider):
    if r is None or r.status_code < 400:
        return
    try:
        err = r.json()
        err = err[0] if isinstance(err, list) else err
        msg = (err.get("error") or {}).get("message") if isinstance(err.get("error"), dict) else err.get("error")
        msg = msg or err.get("message") or r.text
    except ValueError:
        msg = r.text
    msg = str(msg)[:300]
    name = label(provider)
    if r.status_code in (401, 403) or (r.status_code == 400 and re.search(r"api.?key", msg, re.I)):
        raise AIError(f"The {name} API key was rejected. Check it in Settings. ({msg})")
    if r.status_code == 404:
        raise AIError(f"{name} doesn't recognise that model or address. Load the model list again in Settings. ({msg})")
    if r.status_code == 413 or "context" in msg.lower() and "length" in msg.lower():
        raise AIError(f"The transcript is too long for this {name} model. Pick one with a larger context window. ({msg})")
    if r.status_code == 429:
        raise AIError(f"{name} rate limit or quota reached. ({msg})")
    raise AIError(f"{name} error {r.status_code}: {msg}")


def friendly(e: Exception, provider: str) -> str:
    name = label(provider)
    if isinstance(e, AIError):
        return str(e)
    if isinstance(e, anthropic.AuthenticationError):
        return "The Claude API key was rejected. Check it in Settings."
    if isinstance(e, anthropic.RateLimitError):
        return "Claude's rate limit was hit."
    if isinstance(e, anthropic.APIStatusError):
        return f"Claude API error {e.status_code}: {e.message}"
    if isinstance(e, (anthropic.APIConnectionError, httpx.ConnectError, httpx.ConnectTimeout)):
        if provider == "ollama":
            return "Couldn't reach Ollama on this PC. Make sure the Ollama app is installed and running."
        return f"Couldn't reach {name} (check your internet connection)."
    if isinstance(e, httpx.TimeoutException):
        return f"{name} took too long to answer."
    return str(e)


def _parse(text: str) -> dict:
    """JSON from a model's reply, tolerating code fences or a sentence around it."""
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            pass
    raise AIError("The AI's answer wasn't valid JSON. Try again, or pick a stronger model.")
