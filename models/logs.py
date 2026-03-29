from pydantic import BaseModel
from typing import Optional


class CallSummary(BaseModel):
    callId: str
    created: Optional[str] = None
    joined: Optional[str] = None
    ended: Optional[str] = None
    duration: Optional[str] = None        # human-readable e.g. "4m 17s"
    endReason: Optional[str] = None
    shortSummary: Optional[str] = None
    medium: Optional[str] = None          # "plivo" | "webRtc" | etc.


class CallsListResponse(BaseModel):
    total: int
    next: Optional[str] = None            # cursor for next page
    previous: Optional[str] = None
    results: list[CallSummary]


class MessageItem(BaseModel):
    role: str                              # "agent" | "user"
    text: str
    medium: Optional[str] = None          # "voice" | "text"


class CallMessagesResponse(BaseModel):
    callId: str
    total: int
    messages: list[MessageItem]
