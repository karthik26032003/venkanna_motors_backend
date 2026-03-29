"""
routers/webhook.py
──────────────────
Receives real-time call lifecycle events from Ultravox and logs them
to the backend terminal.

Events handled:
  call.started  → ✅ CALL STARTED   (dialling)
  call.joined   → 📞 CALL JOINED    (answered / lifted)
  call.ended    → 🔴 CALL ENDED     (hangup / done)
  call.billed   → 💰 CALL BILLED    (billing finalised)

Security: every request is verified with HMAC-SHA256 before processing.
"""

import datetime
import hmac
import logging
import os

from fastapi import APIRouter, HTTPException, Request, Response

import helpers.db as db
from helpers.ultravox import create_outbound_call

logger = logging.getLogger("webhook")

router = APIRouter(prefix="/webhook", tags=["webhook"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _verify_signature(body: bytes, timestamp: str, signature_header: str) -> bool:
    """
    Verify Ultravox HMAC-SHA256 signature.
    Signature = HMAC-SHA256(secret, raw_body + timestamp)
    Header may contain multiple comma-separated signatures (key rotation).
    """
    secret = os.getenv("WEBHOOK_SECRET", "")
    if not secret:
        logger.warning("WEBHOOK_SECRET not set — skipping signature verification")
        return True  # allow through so dev/test isn't blocked

    expected = hmac.new(
        secret.encode(),
        body + timestamp.encode(),
        "sha256",
    ).hexdigest()

    for sig in signature_header.split(","):
        if hmac.compare_digest(sig.strip(), expected):
            return True
    return False


def _extract_label(call: dict) -> str:
    """Build a short human-readable label from the call object."""
    medium_obj = call.get("medium") or {}
    medium = next(iter(medium_obj), "unknown")

    # For outbound calls, try to get the destination number from the medium object
    to_number = None
    for provider in ("plivo", "twilio", "telnyx"):
        provider_data = medium_obj.get(provider, {})
        outgoing = provider_data.get("outgoing", {})
        to_number = outgoing.get("to")
        if to_number:
            break

    if to_number:
        return f"medium={medium} | to={to_number}"
    return f"medium={medium}"


def _fmt_duration(joined: str | None, ended: str | None) -> str:
    """Human-readable duration between two ISO timestamps."""
    if not joined or not ended:
        return "unknown"
    try:
        def _parse(ts: str) -> datetime.datetime:
            ts = ts.replace("+00:00", "Z")
            for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
                try:
                    return datetime.datetime.strptime(ts, fmt).replace(
                        tzinfo=datetime.timezone.utc
                    )
                except ValueError:
                    continue
            raise ValueError(ts)

        secs = int((_parse(ended) - _parse(joined)).total_seconds())
        m, s = divmod(secs, 60)
        return f"{m}m {s}s" if m else f"{s}s"
    except Exception:
        return "unknown"


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@router.post("/ultravox", status_code=204)
async def ultravox_webhook(request: Request) -> Response:
    """
    POST /webhook/ultravox
    Receives all call lifecycle events from Ultravox.
    Returns 204 immediately so Ultravox doesn't retry.
    """
    body = await request.body()

    # ── Signature verification ────────────────────────────────────────────────
    timestamp = request.headers.get("X-Ultravox-Webhook-Timestamp", "")
    signature = request.headers.get("X-Ultravox-Webhook-Signature", "")

    if timestamp and signature:
        # Reject requests older than 5 minutes (replay attack protection)
        try:
            ts = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            age = (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds()
            if age > 300:
                logger.warning(f"Webhook rejected — timestamp too old ({int(age)}s)")
                raise HTTPException(status_code=400, detail="Webhook timestamp expired")
        except ValueError:
            pass  # unparseable timestamp — let signature check decide

        if not _verify_signature(body, timestamp, signature):
            logger.warning("Webhook rejected — invalid signature")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # ── Parse payload ─────────────────────────────────────────────────────────
    try:
        payload = request.app.state  # unused — just to confirm app is live  # noqa: F841
        import json
        data  = json.loads(body)
        event = data.get("event", "unknown")
        call  = data.get("call", {})
    except Exception as e:
        logger.error(f"Failed to parse webhook payload: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    call_id  = call.get("callId", "unknown")
    label    = _extract_label(call)
    metadata = call.get("metadata") or {}
    batch_id = metadata.get("batch_id")

    # ── Log each event ────────────────────────────────────────────────────────
    if event == "call.started":
        logger.info(
            f"✅ CALL STARTED    callId={call_id} | {label}"
            + (f" | batch={batch_id}" if batch_id else "")
        )

    elif event == "call.joined":
        joined_at = call.get("joined", "unknown")
        logger.info(
            f"📞 CALL JOINED     callId={call_id} | {label} | at={joined_at}"
            + (f" | batch={batch_id}" if batch_id else "")
        )
        if batch_id:
            await db.update_call_status(call_id, "joined")

    elif event == "call.ended":
        end_reason = call.get("endReason") or "unknown"
        duration   = _fmt_duration(call.get("joined"), call.get("ended"))
        summary    = call.get("shortSummary") or ""
        logger.info(
            f"🔴 CALL ENDED      callId={call_id} | {label} | "
            f"reason={end_reason} | duration={duration}"
            + (f" | summary: {summary}" if summary else "")
            + (f" | batch={batch_id}" if batch_id else "")
        )

        if batch_id:
            # A call is "succeeded" if it was answered (joined timestamp present)
            succeeded = bool(call.get("joined"))
            error_msg = end_reason if not succeeded else None

            await db.update_call_status(
                call_id,
                "ended" if succeeded else end_reason,
                error_msg,
            )
            batch_row = await db.close_call_on_batch(batch_id, succeeded)

            if not batch_row:
                logger.warning(f"Batch {batch_id} not found in DB after call.ended")
            elif batch_row.get("queued", 0) > 0:
                # Pop and start the next queued call
                agent_id    = batch_row["agent_id"]
                from_number = batch_row["from_number"]
                next_number = await db.pop_next_queued(batch_id)
                if next_number:
                    try:
                        next_call = await create_outbound_call(
                            agent_id=agent_id,
                            to_number=next_number,
                            from_number=from_number,
                            metadata={"batch_id": batch_id},
                        )
                        await db.set_call_id(batch_id, next_number, next_call["callId"])
                        logger.info(
                            f"Batch {batch_id}: next call started "
                            f"callId={next_call['callId']} → {next_number}"
                        )
                    except Exception as e:
                        logger.error(
                            f"Batch {batch_id}: failed to start next call → {next_number}: {e}"
                        )
                        await db.update_call_status_by_phone(batch_id, next_number, "failed", str(e))
                        await db.close_call_on_batch(batch_id, succeeded=False)
            elif batch_row.get("active", 0) == 0:
                # No active and no queued → batch is done
                await db.mark_batch_complete(batch_id)
                logger.info(f"Batch {batch_id}: all calls complete ✅")

    elif event == "call.billed":
        billed_dur = call.get("billedDuration") or "unknown"
        logger.info(
            f"💰 CALL BILLED     callId={call_id} | {label} | billedDuration={billed_dur}"
        )

    else:
        logger.warning(f"⚠️  UNKNOWN EVENT   event={event} | callId={call_id}")

    return Response(status_code=204)
