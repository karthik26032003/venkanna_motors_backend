import os
import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException

import helpers.db as db
from helpers.ultravox import create_outbound_call
from models.outbound import (
    OutboundCallRequest,
    OutboundCallResponse,
    OutboundBatchRequest,
    BatchStartResponse,
    BatchStatusResponse,
    BatchListItem,
    BatchCallItem,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/outbound", tags=["outbound"])


def _normalize_phone(number: str) -> str:
    """
    Normalize an Indian phone number to E.164 format (+91XXXXXXXXXX).

    Accepts:
      9876543210      → +919876543210
      919876543210    → +919876543210
      +919876543210   → +919876543210
      +91 98765 43210 → +919876543210
    """
    # Strip all spaces, dashes, parentheses
    cleaned = number.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    if cleaned.startswith("+91"):
        digits = cleaned[3:]
    elif cleaned.startswith("91") and len(cleaned) == 12:
        digits = cleaned[2:]
    else:
        digits = cleaned.lstrip("+")

    return f"+91{digits}"


def _get_config() -> tuple[str, str]:
    """Returns (agent_id, from_number) or raises HTTPException."""
    agent_id    = os.getenv("AGENT_ID", "").strip().strip("'\"")
    from_number = os.getenv("TWILIO_FROM_NUMBER", "").strip()

    if not agent_id:
        raise HTTPException(
            status_code=500,
            detail="AGENT_ID is not configured. Restart the server.",
        )
    if not from_number:
        raise HTTPException(
            status_code=500,
            detail="TWILIO_FROM_NUMBER is not configured in .env.",
        )
    return agent_id, from_number


@router.post("/call", response_model=OutboundCallResponse)
async def initiate_outbound_call(body: OutboundCallRequest):
    """
    POST /outbound/call
    Single outbound call to one phone number.
    """
    agent_id, from_number = _get_config()

    to_number = _normalize_phone(body.phone_number)
    logger.info(f"Outbound call → {to_number} (raw: {body.phone_number}) | agent: {agent_id}")

    try:
        call = await create_outbound_call(
            agent_id=agent_id,
            to_number=to_number,
            from_number=from_number,
        )
    except Exception as e:
        logger.error(f"Outbound call failed: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Call initiation failed: {str(e)}",
        )

    logger.info(f"Outbound call initiated: callId={call['callId']} → {body.phone_number}")

    return OutboundCallResponse(
        callId=call["callId"],
        status="initiated",
        to_number=to_number,
        message=f"Calling {to_number}. The AI will connect shortly.",
    )


@router.post("/calls/batch", response_model=BatchStartResponse)
async def initiate_batch_outbound_calls(body: OutboundBatchRequest):
    """
    POST /outbound/calls/batch
    Queue all numbers in the DB, then fire the first CONCURRENCY calls immediately.
    Subsequent calls are triggered by the webhook on each call.ended event.
    Returns a batch_id for polling /outbound/batch/{batch_id}.
    """
    agent_id, from_number = _get_config()

    pool = db.get_pool()
    if not pool:
        raise HTTPException(
            status_code=503,
            detail="Database not available. Batch calls require DATABASE_URL.",
        )

    contacts = body.contacts  # already validated/normalized by Pydantic
    batch_id = str(uuid4())
    total = len(contacts)

    logger.info(f"Batch {batch_id}: {total} contacts | agent: {agent_id}")

    await db.create_batch(batch_id, agent_id, from_number, total, name=body.name)
    await db.insert_batch_calls(
        batch_id,
        [{"phone_number": c.phone_number, "name": c.name, "vehicle": c.vehicle} for c in contacts],
    )

    # Fire the first CONCURRENCY calls immediately
    started = 0
    for _ in range(min(db.get_concurrency(), total)):
        contact = await db.pop_next_queued(batch_id)
        if not contact:
            break
        number = contact["phone_number"]
        try:
            call = await create_outbound_call(
                agent_id=agent_id,
                to_number=number,
                from_number=from_number,
                metadata={"batch_id": batch_id},
            )
            await db.set_call_id(batch_id, number, call["callId"])
            logger.info(f"Batch {batch_id}: started callId={call['callId']} → {number}")
            started += 1
        except Exception as e:
            logger.error(f"Batch {batch_id}: failed to start call → {number}: {e}")
            await db.update_call_status_by_phone(batch_id, number, "failed", str(e))
            await db.close_call_on_batch(batch_id, succeeded=False)

    queued = total - started
    return BatchStartResponse(
        batch_id=batch_id,
        total=total,
        started=started,
        queued=queued,
        message=f"Batch queued. {started} calls active, {queued} waiting.",
    )


@router.get("/batches", response_model=list[BatchListItem])
async def list_batches():
    """
    GET /outbound/batches
    Returns all batches ordered by most recent first.
    """
    rows = await db.list_batches()
    return [
        BatchListItem(
            batch_id=r["batch_id"],
            name=r.get("name") or "",
            status=r["status"],
            total=r["total"],
            active=r["active"],
            queued=r["queued"],
            succeeded=r["succeeded"],
            failed=r["failed"],
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


@router.get("/batch/{batch_id}/calls", response_model=list[BatchCallItem])
async def get_batch_calls(batch_id: str):
    """
    GET /outbound/batch/{batch_id}/calls
    Returns all calls in a batch with their current status.
    """
    rows = await db.get_batch_calls(batch_id)
    if not rows and not await db.get_batch(batch_id):
        raise HTTPException(status_code=404, detail="Batch not found")
    return [
        BatchCallItem(
            id=r["id"],
            phone_number=r["phone_number"],
            customer_name=r.get("customer_name") or "",
            vehicle=r.get("vehicle") or "",
            call_id=r.get("call_id"),
            status=r["status"],
            error=r.get("error"),
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


@router.delete("/batch/{batch_id}", status_code=204)
async def delete_batch(batch_id: str):
    """
    DELETE /outbound/batch/{batch_id}
    Deletes a batch and all its calls. Only allowed for completed batches.
    """
    row = await db.get_batch(batch_id)
    if not row:
        raise HTTPException(status_code=404, detail="Batch not found")
    if row["status"] == "running":
        raise HTTPException(status_code=400, detail="Cannot delete a running batch. Wait for it to complete.")
    await db.delete_batch(batch_id)


@router.get("/batch/{batch_id}", response_model=BatchStatusResponse)
async def get_batch_status(batch_id: str):
    """
    GET /outbound/batch/{batch_id}
    Returns the current status of a batch call job.
    """
    row = await db.get_batch(batch_id)
    if not row:
        raise HTTPException(status_code=404, detail="Batch not found")

    return BatchStatusResponse(
        batch_id=row["batch_id"],
        status=row["status"],
        total=row["total"],
        active=row["active"],
        queued=row["queued"],
        succeeded=row["succeeded"],
        failed=row["failed"],
        created_at=row["created_at"].isoformat(),
    )
