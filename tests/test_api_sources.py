"""POST /sources, /sources/{id}/process のテスト(HANDOVER.md §5 item5-9)。"""

from __future__ import annotations

import uuid

from crm_mvp.api import deps
from crm_mvp.enums import AutonomyMode
from crm_mvp.models import EngagementRole, FieldAutonomyPolicy, GraphNode
from crm_mvp.ports.extractor import ExtractorPort
from crm_mvp.schemas.extraction import ExtractedClaim, ExtractionRequest, ExtractionResult

from .conftest import create_account_and_engagement


class FakeExtractor:
    """常に engagement_role:stance の主張を1件返すテスト用抽出器。"""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        return ExtractionResult(
            claims=[
                ExtractedClaim(
                    target_type="engagement_role", field_path="stance",
                    value={"node_name": "田中", "stance": "supporter"},
                    model_score=0.95, rationale="発言から支持的と判断",
                    evidence_quote="田中さん「前向きに検討したい」",
                ),
            ],
            extractor_version="fake-v1",
        )


def test_create_source_requires_engagement_or_account(api_client):
    resp = api_client.post("/sources", json={"kind": "free_note", "raw_text": "メモ"})
    assert resp.status_code == 422


def test_create_source_success(api_client, db_session, tenant_id):
    _, engagement = create_account_and_engagement(db_session, tenant_id)
    resp = api_client.post("/sources", json={
        "engagement_id": str(engagement.id), "kind": "free_note",
        "raw_text": "テストメモ",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["processed_at"] is None
    assert body["engagement_id"] == str(engagement.id)


def test_process_source_with_null_extractor_yields_no_claims(
    api_client, db_session, tenant_id,
):
    _, engagement = create_account_and_engagement(db_session, tenant_id)
    create_resp = api_client.post("/sources", json={
        "engagement_id": str(engagement.id), "kind": "free_note",
        "raw_text": "特に情報なし",
    })
    source_id = create_resp.json()["id"]

    resp = api_client.post(f"/sources/{source_id}/process", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["claims"] == 0
    assert body["auto_applied"] == 0


def test_process_source_twice_returns_409(api_client, db_session, tenant_id):
    _, engagement = create_account_and_engagement(db_session, tenant_id)
    create_resp = api_client.post("/sources", json={
        "engagement_id": str(engagement.id), "kind": "free_note",
        "raw_text": "メモ",
    })
    source_id = create_resp.json()["id"]

    first = api_client.post(f"/sources/{source_id}/process", json={})
    assert first.status_code == 200
    second = api_client.post(f"/sources/{source_id}/process", json={})
    assert second.status_code == 409


def test_process_recording_without_stt_returns_501(api_client, db_session, tenant_id):
    _, engagement = create_account_and_engagement(db_session, tenant_id)
    create_resp = api_client.post("/sources", json={
        "engagement_id": str(engagement.id), "kind": "recording",
        "uri": "s3://bucket/recording.mp4",
    })
    source_id = create_resp.json()["id"]

    resp = api_client.post(f"/sources/{source_id}/process", json={})
    assert resp.status_code == 501


def test_process_source_auto_applies_when_field_autonomy_is_always_auto(
    api_client, db_session, tenant_id,
):
    from crm_mvp.api.app import app

    app.dependency_overrides[deps.get_extractor] = lambda: FakeExtractor()
    try:
        _, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.add(FieldAutonomyPolicy(
            tenant_id=tenant_id, target_type="engagement_role",
            field_path="stance", mode=AutonomyMode.ALWAYS_AUTO,
        ))
        db_session.flush()

        create_resp = api_client.post("/sources", json={
            "engagement_id": str(engagement.id), "kind": "transcript",
            "raw_text": "田中さん「前向きに検討したい」と発言。",
        })
        source_id = create_resp.json()["id"]

        resp = api_client.post(f"/sources/{source_id}/process", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["claims"] == 1
        assert body["auto_applied"] == 1
        assert body["pending"] == 0

        node = db_session.query(GraphNode).filter_by(
            tenant_id=tenant_id, placeholder_label="田中(氏名未確認)",
        ).one()
        role = db_session.query(EngagementRole).filter_by(
            tenant_id=tenant_id, engagement_id=engagement.id, node_id=node.id,
        ).one()
        assert role.stance == "supporter"
    finally:
        app.dependency_overrides.pop(deps.get_extractor, None)
