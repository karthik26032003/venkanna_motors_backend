import re
from pydantic import BaseModel, field_validator


def _validate_e164(v: str) -> str:
    """
    Accept Indian numbers in any common format and normalize to E.164.
      9876543210      → +919876543210
      919876543210    → +919876543210
      +919876543210   → +919876543210 (unchanged)
    """
    cleaned = v.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    if cleaned.startswith("+91"):
        digits = cleaned[3:]
    elif cleaned.startswith("91") and len(cleaned) == 12:
        digits = cleaned[2:]
    elif re.match(r"^\+\d{7,15}$", cleaned):
        return cleaned  # non-Indian E.164 — pass through as-is
    else:
        digits = cleaned.lstrip("+")

    if not re.match(r"^\d{10}$", digits):
        raise ValueError(
            "Enter a valid 10-digit Indian number, e.g. 9876543210 or +919876543210"
        )
    return f"+91{digits}"


class OutboundCallRequest(BaseModel):
    # Phone number in E.164 format: +[country_code][number]  e.g. +919876543210
    phone_number: str
    jd_text: str = ""

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return _validate_e164(v)


class OutboundCallResponse(BaseModel):
    callId: str
    status: str       # "initiated"
    to_number: str
    message: str


class OutboundBatchRequest(BaseModel):
    phone_numbers: list[str]
    name: str = ""

    @field_validator("phone_numbers")
    @classmethod
    def validate_phones(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("phone_numbers list cannot be empty")
        return [_validate_e164(num) for num in v]


class OutboundBatchResult(BaseModel):
    phone_number: str
    success: bool
    callId: str | None = None
    error: str | None = None


class OutboundBatchResponse(BaseModel):
    total: int
    succeeded: int
    failed: int
    results: list[OutboundBatchResult]


# ── DB-backed batch queue models ──────────────────────────────────────────────

class BatchStartResponse(BaseModel):
    batch_id: str
    total: int
    started: int
    queued: int
    message: str


class BatchStatusResponse(BaseModel):
    batch_id: str
    status: str        # running | completed
    total: int
    active: int
    queued: int
    succeeded: int
    failed: int
    created_at: str


class BatchListItem(BaseModel):
    batch_id: str
    name: str
    status: str
    total: int
    active: int
    queued: int
    succeeded: int
    failed: int
    created_at: str


class BatchCallItem(BaseModel):
    id: int
    phone_number: str
    call_id: str | None
    status: str
    error: str | None
    created_at: str
