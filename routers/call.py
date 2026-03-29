import os
import logging

from fastapi import APIRouter, HTTPException

from helpers.ultravox import create_agent_call
from models.call import CallStartRequest, CallStartResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/call", tags=["call"])


@router.post("/start", response_model=CallStartResponse)
async def start_call(body: CallStartRequest):
    """
    POST /call/start
    ─────────────────────────────────────────────────────────────────
    Triggered when the user clicks "Start Call" in the browser.

    Flow:
      1. Reads AGENT_ID from env
      2. Calls POST https://api.ultravox.ai/api/agents/{agent_id}/calls
         with templateContext: { jd_content: body.jd_text }
         → Ultravox fills {{{jd_content}}} in the agent's system prompt
      3. Ultravox returns { callId, joinUrl, ... }
      4. We return { callId, joinUrl } to the frontend
      5. Frontend SDK calls UltravoxSession.joinCall(joinUrl) → WebRTC live
    """
    agent_id = os.getenv("AGENT_ID", "").strip().strip("'\"")

    if not agent_id:
        raise HTTPException(
            status_code=500,
            detail="AGENT_ID is not configured. Restart the server to auto-create the agent.",
        )

    logger.info(f"Starting call — agent: {agent_id}")

    try:
        call = await create_agent_call(
            agent_id=agent_id,
            metadata=body.metadata,
        )
    except Exception as e:
        logger.error(f"Failed to create call: {e}")
        raise HTTPException(status_code=502, detail=f"Ultravox API error: {str(e)}")

    logger.info(f"Call created: callId={call['callId']}")

    return CallStartResponse(
        callId=call["callId"],
        joinUrl=call["joinUrl"],
    )
