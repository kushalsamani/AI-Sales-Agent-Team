"""
tools/llm.py
------------
Shared Gemini LLM client used by all agents.

Centralising the client here means:
  - One place to swap models or providers in the future.
  - No duplicate client initialisation across agent files.
  - All LLM calls go through generate_json(), enforcing structured output
    and keeping token usage predictable.

To change the model, update GEMINI_MODEL in .env — no code changes needed.
"""

import json
from google import genai
from google.genai import types

import config

# Lazy singleton — created on first call, reused for all subsequent calls.
_client: genai.Client | None = None


def get_client() -> genai.Client:
    """
    Return the shared Gemini client, initialising it on first call.

    Raises:
        ValueError: If GEMINI_API_KEY is missing from .env.
    """
    global _client
    if _client is None:
        config.require_key(
            "GEMINI_API_KEY",
            config.GEMINI_API_KEY,
            "Get a free key at: https://aistudio.google.com/app/apikey"
        )
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def generate_json(prompt: str, temperature: float = 0.2) -> dict | list:
    """
    Send a prompt to Gemini and return the response parsed as JSON.

    Using response_mime_type='application/json' forces the model to return
    valid JSON, eliminating markdown fences or prose wrapping.

    Args:
        prompt:      The full prompt string to send.
        temperature: Sampling temperature.
                     Use 0.1–0.2 for factual/structured tasks (research, validation).
                     Use 0.3–0.4 for generative tasks (query generation).

    Returns:
        Parsed response as a dict or list, depending on what the prompt requested.

    Raises:
        ValueError: If the response cannot be parsed as JSON (shouldn't happen
                    with response_mime_type enforced, but handled defensively).
    """
    client = get_client()

    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=temperature,
        ),
    )

    try:
        return json.loads(response.text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"[LLM ERROR] Gemini returned non-JSON output.\n"
            f"First 400 chars: {response.text[:400]}\n"
            f"Parse error: {e}"
        )
