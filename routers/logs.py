import os
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from helpers.ultravox import get_agent_calls, get_call_messages, get_call_recording
import helpers.db as db
from models.logs import CallsListResponse, CallSummary, CallMessagesResponse, MessageItem

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/logs", tags=["logs"])

# Roles we want to surface in the transcript (strip tool calls/results)
_VISIBLE_ROLES = {"MESSAGE_ROLE_USER", "MESSAGE_ROLE_AGENT"}
_ROLE_MAP = {
    "MESSAGE_ROLE_USER":  "user",
    "MESSAGE_ROLE_AGENT": "agent",
}
_MEDIUM_MAP = {
    "MESSAGE_MEDIUM_VOICE": "voice",
    "MESSAGE_MEDIUM_TEXT":  "text",
}


def _parse_duration(joined: str | None, ended: str | None, billed: str | None) -> str | None:
    """Return a human-readable duration string."""
    # Prefer billedDuration (e.g. "257.3s") if available
    if billed:
        try:
            secs = float(billed.rstrip("s"))
            m, s = divmod(int(secs), 60)
            return f"{m}m {s}s" if m else f"{s}s"
        except ValueError:
            pass

    # Fallback: calculate from joined / ended timestamps
    if joined and ended:
        try:
            def _parse(ts: str):
                ts = ts.replace("+00:00", "Z")
                for f in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
                    try:
                        return datetime.strptime(ts, f).replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                return None
            t_start = _parse(joined)
            t_end   = _parse(ended)
            if t_start and t_end:
                secs = int((t_end - t_start).total_seconds())
                m, s = divmod(secs, 60)
                return f"{m}m {s}s" if m else f"{s}s"
        except Exception:
            pass

    return None


def _extract_medium(medium_obj: dict | None) -> str | None:
    """Return the telephony medium name from the medium object."""
    if not medium_obj:
        return None
    for key in ("webRtc", "plivo", "twilio", "telnyx", "exotel", "sip", "webSocket"):
        if key in medium_obj:
            return key
    return None


def _cursor_from_url(url: str | None) -> str | None:
    """Extract cursor token from a paginated URL."""
    if not url:
        return None
    for part in url.split("&"):
        if "cursor=" in part:
            return part.split("cursor=")[-1]
    return None


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp to a timezone-aware datetime."""
    if not ts:
        return None
    try:
        ts = ts.replace("+00:00", "Z")
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(ts, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    except Exception:
        pass
    return None


async def _enrich_with_contact_info(summaries: list[CallSummary]) -> None:
    """Mutates each CallSummary in-place with DB-stored fields (batch calls only)."""
    call_ids    = [s.callId for s in summaries]
    contact_map = await db.get_contact_info_by_call_ids(call_ids)
    for s in summaries:
        info = contact_map.get(s.callId)
        if info:
            s.phone_number  = info["phone_number"]
            s.customer_name = info["customer_name"]
            s.vehicle       = info["vehicle"]
            s.sentiment     = info["sentiment"]
            s.takeaway      = info["takeaway"]
            s.callback      = info["callback"]


@router.get("/calls", response_model=CallsListResponse)
async def list_calls(
    page_size:  int          = Query(20, ge=1, le=100),
    cursor:     str | None   = Query(None),
    date_from:  str | None   = Query(None, description="ISO date, e.g. 2026-01-01"),
    date_to:    str | None   = Query(None, description="ISO date, e.g. 2026-01-31"),
    medium:     str | None   = Query(None, description="e.g. plivo, webRtc, twilio"),
):
    """
    GET /logs/calls
    Returns calls for the configured agent, newest first.
    When date_from / date_to / medium filters are supplied the backend fetches
    pages from Ultravox until the date window is exhausted (max 50 pages) and
    returns all matching results in one shot (no cursor).
    Without filters, normal cursor-based pagination applies.
    """
    agent_id = os.getenv("AGENT_ID", "").strip().strip("'\"")
    if not agent_id:
        raise HTTPException(status_code=500, detail="AGENT_ID is not configured.")

    filtering = bool(date_from or date_to or medium)

    dt_from = _parse_iso(date_from)
    dt_to   = _parse_iso(date_to)
    # date_to means end of that day
    if dt_to:
        dt_to = dt_to.replace(hour=23, minute=59, second=59, microsecond=999999)

    def _to_summary(call: dict) -> CallSummary:
        return CallSummary(
            callId       = call.get("callId", ""),
            created      = call.get("created"),
            joined       = call.get("joined"),
            ended        = call.get("ended"),
            duration     = _parse_duration(
                               call.get("joined"),
                               call.get("ended"),
                               call.get("billedDuration"),
                           ),
            endReason    = call.get("endReason"),
            shortSummary = call.get("shortSummary"),
            medium       = _extract_medium(call.get("medium")),
        )

    # ── Filtered mode: fetch pages until date window exhausted ───────────────
    if filtering:
        MAX_PAGES   = 50
        results     = []
        page_cursor = None  # always start from newest
        done        = False

        for _ in range(MAX_PAGES):
            try:
                data = await get_agent_calls(agent_id, cursor=page_cursor, page_size=100)
            except Exception as e:
                logger.error(f"Failed to fetch calls: {e}")
                raise HTTPException(status_code=502, detail=f"Ultravox API error: {str(e)}")

            for call in data.get("results", []):
                summary   = _to_summary(call)
                call_time = _parse_iso(summary.created)

                # Calls come newest-first; once we're older than date_from, stop.
                if dt_from and call_time and call_time < dt_from:
                    done = True
                    break

                # Skip calls newer than date_to
                if dt_to and call_time and call_time > dt_to:
                    continue

                # Filter by medium
                if medium and summary.medium != medium:
                    continue

                results.append(summary)

            if done or not data.get("next"):
                break
            page_cursor = _cursor_from_url(data.get("next"))

        await _enrich_with_contact_info(results)
        return CallsListResponse(
            total    = len(results),
            next     = None,
            previous = None,
            results  = results,
        )

    # ── Normal paginated mode ────────────────────────────────────────────────
    try:
        data = await get_agent_calls(agent_id, cursor=cursor, page_size=page_size)
    except Exception as e:
        logger.error(f"Failed to fetch calls: {e}")
        raise HTTPException(status_code=502, detail=f"Ultravox API error: {str(e)}")

    results = [_to_summary(call) for call in data.get("results", [])]
    await _enrich_with_contact_info(results)

    return CallsListResponse(
        total    = data.get("total", len(results)),
        next     = _cursor_from_url(data.get("next")),
        previous = _cursor_from_url(data.get("previous")),
        results  = results,
    )


@router.get("/usage")
async def get_usage(
    year:  int = Query(..., description="e.g. 2026"),
    month: int = Query(..., ge=1, le=12, description="1-12"),
):
    """
    GET /logs/usage?year=2026&month=3
    Returns aggregated usage stats for the given calendar month.
    date_from = 1st of month 00:00:00 UTC
    date_to   = last day of month 23:59:59 UTC
    """
    import calendar

    agent_id = os.getenv("AGENT_ID", "").strip().strip("'\"")
    if not agent_id:
        raise HTTPException(status_code=500, detail="AGENT_ID is not configured.")

    # Build date window for the full calendar month
    last_day = calendar.monthrange(year, month)[1]
    date_from = f"{year}-{month:02d}-01"
    date_to   = f"{year}-{month:02d}-{last_day:02d}"

    dt_from = _parse_iso(date_from)
    dt_to   = _parse_iso(date_to)
    if dt_to:
        dt_to = dt_to.replace(hour=23, minute=59, second=59, microsecond=999999)

    # Fetch all calls in the month
    MAX_PAGES   = 50
    all_results = []
    page_cursor = None

    for _ in range(MAX_PAGES):
        try:
            data = await get_agent_calls(agent_id, cursor=page_cursor, page_size=100)
        except Exception as e:
            logger.error(f"Failed to fetch calls for usage: {e}")
            raise HTTPException(status_code=502, detail=f"Ultravox API error: {str(e)}")

        done = False
        for call in data.get("results", []):
            call_time = _parse_iso(call.get("created"))
            if dt_from and call_time and call_time < dt_from:
                done = True
                break
            if dt_to and call_time and call_time > dt_to:
                continue
            all_results.append(call)

        if done or not data.get("next"):
            break
        page_cursor = _cursor_from_url(data.get("next"))

    # Aggregate stats
    total_calls   = len(all_results)
    joined_calls  = sum(1 for c in all_results if c.get("joined"))
    join_rate     = round(joined_calls / total_calls * 100) if total_calls else 0

    total_billed_secs = 0.0
    for c in all_results:
        bd = c.get("billedDuration")
        if bd:
            try:
                total_billed_secs += float(str(bd).rstrip("s"))
            except ValueError:
                pass

    total_billed_min = round(total_billed_secs / 60, 1)
    avg_dur_secs     = round(total_billed_secs / joined_calls) if joined_calls else 0

    # Daily breakdown — keyed by YYYY-MM-DD
    daily: dict[str, dict] = {}
    for c in all_results:
        day = (c.get("created") or "")[:10]
        if not day:
            continue
        if day not in daily:
            daily[day] = {"calls": 0, "billed_min": 0.0}
        daily[day]["calls"] += 1
        bd = c.get("billedDuration")
        if bd:
            try:
                daily[day]["billed_min"] += float(str(bd).rstrip("s")) / 60
            except ValueError:
                pass

    # Fill every day of the month (including 0s for days with no calls)
    daily_list = []
    for d in range(1, last_day + 1):
        day_str = f"{year}-{month:02d}-{d:02d}"
        entry   = daily.get(day_str, {"calls": 0, "billed_min": 0.0})
        daily_list.append({
            "date":       day_str,
            "calls":      entry["calls"],
            "billed_min": round(entry["billed_min"], 1),
        })

    return {
        "year":             year,
        "month":            month,
        "total_calls":      total_calls,
        "joined_calls":     joined_calls,
        "join_rate":        join_rate,
        "total_billed_min": total_billed_min,
        "avg_dur_secs":     avg_dur_secs,
        "daily":            daily_list,
        "days_in_month":    last_day,
    }


@router.get("/calls/{call_id}/messages", response_model=CallMessagesResponse)
async def get_messages(call_id: str):
    """
    GET /logs/calls/{call_id}/messages
    Returns the cleaned transcript (agent + user only) for a single call.
    """
    try:
        data = await get_call_messages(call_id)
    except Exception as e:
        logger.error(f"Failed to fetch messages for {call_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Ultravox API error: {str(e)}")

    messages = []
    for msg in data.get("results", []):
        role = msg.get("role", "")
        if role not in _VISIBLE_ROLES:
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        messages.append(MessageItem(
            role   = _ROLE_MAP[role],
            text   = text,
            medium = _MEDIUM_MAP.get(msg.get("medium", ""), None),
        ))

    return CallMessagesResponse(
        callId   = call_id,
        total    = len(messages),
        messages = messages,
    )


@router.get("/calls/{call_id}/recording")
async def get_recording(call_id: str, download: int = Query(0)):
    """
    GET /logs/calls/{call_id}/recording
    Proxies the recording from Ultravox back to the client.

    ?download=1  → Content-Disposition: attachment  (browser saves file)
    default      → Content-Disposition: inline       (browser plays in <audio>)
    """
    try:
        resp = await get_call_recording(call_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (404, 422):
            raise HTTPException(status_code=404, detail="Recording not available for this call.")
        logger.error(f"Failed to fetch recording for {call_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Ultravox API error: {str(e)}")
    except Exception as e:
        logger.error(f"Failed to fetch recording for {call_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Ultravox API error: {str(e)}")

    content_type = resp.headers.get("content-type", "audio/wav")
    ext = "mp3" if "mpeg" in content_type else "wav"
    filename = f"recording_{call_id}.{ext}"
    disposition = f'attachment; filename="{filename}"' if download else f'inline; filename="{filename}"'

    def _iter():
        yield resp.content

    return StreamingResponse(
        _iter(),
        media_type=content_type,
        headers={
            "Content-Disposition": disposition,
            "Content-Length": str(len(resp.content)),
            "Accept-Ranges": "bytes",
        },
    )
