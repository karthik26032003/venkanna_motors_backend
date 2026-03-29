from pydantic import BaseModel
from typing import Optional


class CallTemplate(BaseModel):
    systemPrompt: str
    model: str = "fixie-ai/ultravox-70B"
    voice: str = "Chinmay-English-Indian"
    languageHint: str = "en"
    maxDuration: str = "3600s"
    firstSpeakerSettings: dict = {"agent": {}}


class AgentCreateRequest(BaseModel):
    name: str
    callTemplate: CallTemplate


class AgentCreateResponse(BaseModel):
    agentId: str
    name: str
    created: str
    publishedRevisionId: Optional[str] = None
