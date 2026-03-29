import httpx
import os

ULTRAVOX_BASE_URL = "https://api.ultravox.ai/api"


def _headers() -> dict:
    api_key = os.getenv("ULTRAVOX_API_KEY")
    if not api_key or api_key == "your_ultravox_api_key_here":
        raise ValueError("ULTRAVOX_API_KEY is not set in .env")
    return {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }


def _rag_tools(corpus_id: str) -> list:
    """Returns the selectedTools list for the queryCorpus RAG tool."""
    return [
        {
            "toolName": "queryCorpus",
            "parameterOverrides": {
                "corpus_id": corpus_id,
                "max_results": 5,
            },
        },
    ]


async def create_agent(
    name: str,
    system_prompt: str,
    voice: str,
    model: str,
    # language_hint: str,
    max_duration: str,
    corpus_id: str,
) -> dict:
    """
    POST https://api.ultravox.ai/api/agents
    Creates a new agent with the given config.
    Returns the full agent object including agentId.
    """
    payload = {
        "name": name,
        "callTemplate": {
            "systemPrompt": system_prompt,
            "model": model,
            "voice": voice,
            # "languageHint": language_hint,
            "maxDuration": max_duration,
            "recordingEnabled": True,
            "firstSpeakerSettings": {"agent": {}},
            "selectedTools": _rag_tools(corpus_id),
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{ULTRAVOX_BASE_URL}/agents",
            headers=_headers(),
            json=payload,
        )
        response.raise_for_status()
        return response.json()


async def create_agent_call(
    agent_id: str,
    metadata: dict | None = None,
) -> dict:
    """
    POST https://api.ultravox.ai/api/agents/{agent_id}/calls
    Starts a new call session under the given agent.
    Returns the Call object — most importantly joinUrl (used by the frontend SDK).

    JD content is fetched via the queryCorpus RAG tool during the call —
    no templateContext injection needed.
    medium: webRtc → browser joins via WebRTC using the ultravox-client SDK.
    """
    payload: dict = {
        "medium": {"webRtc": {}},
        "recordingEnabled": True,
    }
    if metadata:
        payload["metadata"] = metadata

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{ULTRAVOX_BASE_URL}/agents/{agent_id}/calls",
            headers=_headers(),
            json=payload,
        )
        response.raise_for_status()
        return response.json()


async def patch_agent(
    agent_id: str,
    system_prompt: str,
    voice: str,
    model: str,
    # language_hint: str,
    max_duration: str,
    corpus_id: str,
) -> dict:
    """
    PATCH https://api.ultravox.ai/api/agents/{agent_id}
    Syncs the full callTemplate on every backend startup so that voice, model,
    selectedTools (RAG), and all other settings always reflect the current .env.
    """
    payload = {
        "callTemplate": {
            "systemPrompt": system_prompt,
            "voice": voice,
            "model": model,
            # "languageHint": language_hint,
            "maxDuration": max_duration,
            "recordingEnabled": True,
            "firstSpeakerSettings": {"agent": {}},
            "selectedTools": _rag_tools(corpus_id),
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.patch(
            f"{ULTRAVOX_BASE_URL}/agents/{agent_id}",
            headers=_headers(),
            json=payload,
        )
        response.raise_for_status()
        return response.json()


async def create_outbound_call(
    agent_id: str,
    to_number: str,
    from_number: str,
    metadata: dict | None = None,
) -> dict:
    """
    POST https://api.ultravox.ai/api/agents/{agent_id}/calls
    Creates an outbound phone call via Plivo.

    Ultravox uses your registered Plivo credentials to dial to_number.
    Audio flows: phone ↔ Plivo ↔ Ultravox AI.
    """
    payload: dict = {
        "medium": {
            "plivo": {
                "outgoing": {
                    "to": to_number,
                    "from": from_number,
                }
            }
        },
        "recordingEnabled": True,
    }
    if metadata:
        payload["metadata"] = metadata

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{ULTRAVOX_BASE_URL}/agents/{agent_id}/calls",
            headers=_headers(),
            json=payload,
        )
        response.raise_for_status()
        return response.json()


async def get_agent_calls(
    agent_id: str,
    cursor: str | None = None,
    page_size: int = 20,
) -> dict:
    """
    GET /api/agents/{agent_id}/calls
    Paginated list of all calls under this agent.
    """
    params: dict = {"pageSize": page_size}
    if cursor:
        params["cursor"] = cursor

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{ULTRAVOX_BASE_URL}/agents/{agent_id}/calls",
            headers=_headers(),
            params=params,
        )
        response.raise_for_status()
        return response.json()


async def get_call_messages(
    call_id: str,
    cursor: str | None = None,
    page_size: int = 100,
) -> dict:
    """
    GET /api/calls/{call_id}/messages?mode=in_call
    Full conversation transcript across all stages.
    """
    params: dict = {"pageSize": page_size, "mode": "in_call"}
    if cursor:
        params["cursor"] = cursor

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{ULTRAVOX_BASE_URL}/calls/{call_id}/messages",
            headers=_headers(),
            params=params,
        )
        response.raise_for_status()
        return response.json()


async def get_call_recording(call_id: str) -> httpx.Response:
    """
    GET /api/calls/{call_id}/recording
    Returns the raw httpx Response containing the WAV audio (following any redirects).
    Caller is responsible for streaming the content.
    """
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        response = await client.get(
            f"{ULTRAVOX_BASE_URL}/calls/{call_id}/recording",
            headers=_headers(),
        )
        response.raise_for_status()
        return response


async def get_call(call_id: str) -> dict:
    """
    GET https://api.ultravox.ai/api/calls/{call_id}
    Returns the full call object including joined, ended, endReason.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{ULTRAVOX_BASE_URL}/calls/{call_id}",
            headers=_headers(),
        )
        response.raise_for_status()
        return response.json()


async def get_agent(agent_id: str) -> dict:
    """
    GET https://api.ultravox.ai/api/agents/{agent_id}
    Fetches an existing agent's details.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{ULTRAVOX_BASE_URL}/agents/{agent_id}",
            headers=_headers(),
        )
        response.raise_for_status()
        return response.json()


async def list_webhooks(agent_id: str) -> list:
    """
    GET https://api.ultravox.ai/api/webhooks?agentId={agent_id}
    Returns existing webhooks scoped to this agent.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{ULTRAVOX_BASE_URL}/webhooks",
            headers=_headers(),
            params={"agentId": agent_id},
        )
        response.raise_for_status()
        return response.json().get("results", [])


async def register_webhook(url: str, agent_id: str, secret: str) -> dict:
    """
    POST https://api.ultravox.ai/api/webhooks
    Registers a webhook for all call lifecycle events, scoped to agent_id.
    Returns the created webhook object (includes webhookId).
    """
    payload = {
        "url": url,
        "agentId": agent_id,
        "events": ["call.started", "call.joined", "call.ended", "call.billed"],
        "secrets": [secret],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{ULTRAVOX_BASE_URL}/webhooks",
            headers=_headers(),
            json=payload,
        )
        response.raise_for_status()
        return response.json()
