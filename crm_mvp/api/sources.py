"""取り込み API(HANDOVER.md §5 Phase2, item 5-9)。

POST /sources が唯一の入力口(§3.9)。POST /sources/{id}/process は
本来「ワーカー」が非同期にやる仕事(items 6-9)を同期実行する暫定エンドポイント
— outbox/キュー基盤が無いこの MVP での代替。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import SourceKind
from ..models import IngestionSource
from ..ports.extractor import ExtractorPort
from ..ports.stt import STTPort, UnavailableSTT
from ..services.ingestion_runner import ProcessingOutcome, process_source
from .deps import get_extractor, get_session, get_tenant_id

router = APIRouter(prefix="/sources", tags=["sources"])


class SourceCreate(BaseModel):
    engagement_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    kind: SourceKind
    uri: str | None = None
    raw_text: str | None = None
    occurred_at: datetime | None = None
    duration_sec: int | None = None


class SourceOut(BaseModel):
    id: uuid.UUID
    kind: SourceKind
    engagement_id: uuid.UUID | None
    account_id: uuid.UUID | None
    processed_at: datetime | None


class ProcessRequest(BaseModel):
    speakers: list[dict] = Field(default_factory=list)


class ProcessResult(BaseModel):
    claims: int
    auto_applied: int
    pending: int
    discarded: int
    matched_speakers: int


@router.post("", response_model=SourceOut, status_code=201)
def create_source(
    body: SourceCreate,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    session: Session = Depends(get_session),
) -> IngestionSource:
    if body.engagement_id is None and body.account_id is None:
        raise HTTPException(
            status_code=422,
            detail="engagement_id or account_id is required",
        )
    if body.kind != SourceKind.RECORDING and not body.uri and not body.raw_text:
        raise HTTPException(
            status_code=422, detail="uri or raw_text is required",
        )

    source = IngestionSource(tenant_id=tenant_id, **body.model_dump())
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


@router.post("/{source_id}/process", response_model=ProcessResult)
def process_source_endpoint(
    source_id: uuid.UUID,
    body: ProcessRequest,
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    session: Session = Depends(get_session),
    extractor: ExtractorPort = Depends(get_extractor),
) -> ProcessingOutcome:
    source = session.execute(
        select(IngestionSource).where(
            IngestionSource.tenant_id == tenant_id,
            IngestionSource.id == source_id,
        )
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    if source.processed_at is not None:
        raise HTTPException(status_code=409, detail="source already processed")

    stt: STTPort = UnavailableSTT()
    if source.kind == SourceKind.RECORDING and not source.raw_text:
        if not source.uri:
            raise HTTPException(
                status_code=422, detail="recording source has no uri",
            )
        try:
            source.raw_text = stt.transcribe(source.uri)
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc

    try:
        outcome = process_source(
            session, tenant_id, source, extractor=extractor, speakers=body.speakers,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    session.commit()
    return outcome
