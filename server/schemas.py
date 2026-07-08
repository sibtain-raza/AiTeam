"""Pydantic request/response shapes for the API."""

from datetime import datetime

from pydantic import BaseModel, field_validator


class SignupRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _looks_like_email(cls, v: str) -> str:
        if "@" not in v or len(v) < 3:
            raise ValueError("not a valid email address")
        return v.lower()

    @field_validator("password")
    @classmethod
    def _min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("password must be at least 8 characters")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class CreateRunRequest(BaseModel):
    goal: str

    @field_validator("goal")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("goal cannot be empty")
        return v.strip()


class RunSummary(BaseModel):
    id: str
    goal: str
    status: str
    stop_reason: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RunEventOut(BaseModel):
    id: str
    seq: int
    at: datetime
    source: str
    event_type: str
    detail: str
    extra: str | None

    model_config = {"from_attributes": True}
