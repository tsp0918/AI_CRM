"""party_compliance.py(取引先・エンドユーザーのworse-case-winsゲート)の
テスト(CRM_連携引き継ぎ書.md §5.2・§8.1・§8.3)。
"""

from __future__ import annotations

import uuid

from crm_mvp.enums import ComplianceCheckType, ComplianceOutcome
from crm_mvp.models import Account, ComplianceStatus
from crm_mvp.services.party_compliance import (
    check_party_clearance, worst_compliance_outcome,
)


def make_account(db_session, tenant_id, name="テスト取引先") -> Account:
    account = Account(tenant_id=tenant_id, name=name)
    db_session.add(account)
    db_session.flush()
    return account


def add_status(db_session, tenant_id, account_id, check_type, outcome) -> None:
    db_session.add(ComplianceStatus(
        tenant_id=tenant_id, account_id=account_id, check_type=check_type,
        outcome=outcome,
    ))
    db_session.flush()


class TestWorstComplianceOutcome:
    def test_no_checks_returns_none(self, db_session, tenant_id):
        account = make_account(db_session, tenant_id)
        assert worst_compliance_outcome(db_session, tenant_id, account.id) is None

    def test_single_clear(self, db_session, tenant_id):
        account = make_account(db_session, tenant_id)
        add_status(
            db_session, tenant_id, account.id, ComplianceCheckType.ANTI_SOCIAL,
            ComplianceOutcome.CLEAR,
        )
        assert worst_compliance_outcome(db_session, tenant_id, account.id) == ComplianceOutcome.CLEAR

    def test_hit_wins_over_clear(self, db_session, tenant_id):
        account = make_account(db_session, tenant_id)
        add_status(
            db_session, tenant_id, account.id, ComplianceCheckType.ANTI_SOCIAL,
            ComplianceOutcome.CLEAR,
        )
        add_status(
            db_session, tenant_id, account.id, ComplianceCheckType.SANCTIONS,
            ComplianceOutcome.HIT,
        )
        assert worst_compliance_outcome(db_session, tenant_id, account.id) == ComplianceOutcome.HIT

    def test_needs_review_wins_over_clear_but_not_hit(self, db_session, tenant_id):
        account = make_account(db_session, tenant_id)
        add_status(
            db_session, tenant_id, account.id, ComplianceCheckType.CREDIT,
            ComplianceOutcome.NEEDS_REVIEW,
        )
        add_status(
            db_session, tenant_id, account.id, ComplianceCheckType.EXPORT_CONTROL,
            ComplianceOutcome.CLEAR,
        )
        assert worst_compliance_outcome(
            db_session, tenant_id, account.id,
        ) == ComplianceOutcome.NEEDS_REVIEW


class TestCheckPartyClearance:
    def test_both_clear_allows(self, db_session, tenant_id):
        counterparty = make_account(db_session, tenant_id, "取引先")
        end_user = make_account(db_session, tenant_id, "エンドユーザー")
        add_status(
            db_session, tenant_id, counterparty.id, ComplianceCheckType.SANCTIONS,
            ComplianceOutcome.CLEAR,
        )
        add_status(
            db_session, tenant_id, end_user.id, ComplianceCheckType.SANCTIONS,
            ComplianceOutcome.CLEAR,
        )
        assert check_party_clearance(
            db_session, tenant_id, account_id=counterparty.id,
            end_user_account_id=end_user.id,
        ) is None

    def test_no_checks_on_file_allows(self, db_session, tenant_id):
        counterparty = make_account(db_session, tenant_id)
        assert check_party_clearance(
            db_session, tenant_id, account_id=counterparty.id,
        ) is None

    def test_counterparty_hit_blocks(self, db_session, tenant_id):
        counterparty = make_account(db_session, tenant_id, "危険取引先")
        add_status(
            db_session, tenant_id, counterparty.id, ComplianceCheckType.SANCTIONS,
            ComplianceOutcome.HIT,
        )
        reason = check_party_clearance(db_session, tenant_id, account_id=counterparty.id)
        assert reason is not None
        assert "危険取引先" in reason
        assert "取引先" in reason

    def test_end_user_hit_blocks_even_when_counterparty_clear(self, db_session, tenant_id):
        counterparty = make_account(db_session, tenant_id, "取引先")
        end_user = make_account(db_session, tenant_id, "危険エンドユーザー")
        add_status(
            db_session, tenant_id, counterparty.id, ComplianceCheckType.SANCTIONS,
            ComplianceOutcome.CLEAR,
        )
        add_status(
            db_session, tenant_id, end_user.id, ComplianceCheckType.SANCTIONS,
            ComplianceOutcome.HIT,
        )
        reason = check_party_clearance(
            db_session, tenant_id, account_id=counterparty.id,
            end_user_account_id=end_user.id,
        )
        assert reason is not None
        assert "危険エンドユーザー" in reason
        assert "エンドユーザー" in reason

    def test_end_user_none_only_checks_counterparty(self, db_session, tenant_id):
        counterparty = make_account(db_session, tenant_id)
        add_status(
            db_session, tenant_id, counterparty.id, ComplianceCheckType.SANCTIONS,
            ComplianceOutcome.CLEAR,
        )
        assert check_party_clearance(
            db_session, tenant_id, account_id=counterparty.id, end_user_account_id=None,
        ) is None

    def test_end_user_same_as_counterparty_is_not_double_checked(self, db_session, tenant_id):
        counterparty = make_account(db_session, tenant_id)
        add_status(
            db_session, tenant_id, counterparty.id, ComplianceCheckType.SANCTIONS,
            ComplianceOutcome.CLEAR,
        )
        assert check_party_clearance(
            db_session, tenant_id, account_id=counterparty.id,
            end_user_account_id=counterparty.id,
        ) is None

    def test_needs_review_does_not_block(self, db_session, tenant_id):
        """今回のスコープはHITのみをブロック対象とする(Context参照)。"""
        counterparty = make_account(db_session, tenant_id)
        add_status(
            db_session, tenant_id, counterparty.id, ComplianceCheckType.CREDIT,
            ComplianceOutcome.NEEDS_REVIEW,
        )
        assert check_party_clearance(db_session, tenant_id, account_id=counterparty.id) is None
