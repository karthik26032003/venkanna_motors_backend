"""
helpers/openai_helper.py
─────────────────────────
Sends a call transcript to GPT-4o-mini and returns a structured analysis.
Uses httpx directly — no openai SDK needed.
"""

import json
import logging
import os

import httpx

logger = logging.getLogger("openai_helper")

_SYSTEM_PROMPT = """\
You are analyzing a post-service feedback call transcript for a Hero MotoCorp dealership.
Return ONLY valid JSON with exactly these three keys:

- "sentiment": one of "positive", "negative", or "neutral"
- "takeaway": a concise 5-6 word statement capturing the call outcome \
(e.g. "Customer satisfied, will revisit soon")
- "callback": true if a human agent should follow up with this customer, false otherwise

Do not include any explanation outside the JSON object."""


async def analyze_transcript(transcript_text: str) -> dict:
    """
    Calls GPT-4o-mini to analyze the transcript.
    Returns {"sentiment": str, "takeaway": str, "callback": bool}
    Falls back to neutral defaults if OPENAI_API_KEY is not set or the call fails.
    """
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.warning("OPENAI_API_KEY not set — skipping transcript analysis")
        return {"sentiment": "neutral", "takeaway": "Analysis unavailable", "callback": False}

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": f"Analyze this call transcript:\n\n{transcript_text}"},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 150,
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            result  = json.loads(content)
            return {
                "sentiment": str(result.get("sentiment", "neutral")).lower(),
                "takeaway":  str(result.get("takeaway",  "")).strip(),
                "callback":  bool(result.get("callback", False)),
            }
    except Exception as e:
        logger.error(f"OpenAI analysis failed: {e}")
        return {"sentiment": "neutral", "takeaway": "", "callback": False}
