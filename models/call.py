from pydantic import BaseModel
from typing import Optional


class CallStartRequest(BaseModel):
    jd_text: Optional[str] = ""
    metadata: Optional[dict[str, str]] = None


class CallStartResponse(BaseModel):
    callId: str
    joinUrl: str


class CallEndResponse(BaseModel):
    message: str
    callId: str
