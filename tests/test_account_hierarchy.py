"""services/account_hierarchy.py のユニットテスト。"""

from __future__ import annotations

import uuid

import pytest

from crm_mvp.models import Account
from crm_mvp.services import account_hierarchy as ah


def make_account(db_session, tenant_id, **overrides) -> Account:
    defaults = dict(tenant_id=tenant_id, name="テスト取引先")
    defaults.update(overrides)
    account = Account(**defaults)
    db_session.add(account)
    db_session.flush()
    return account


class TestCreateGroupingAccount:
    def test_creates_top_level_account(self, db_session, tenant_id):
        account = ah.create_grouping_account(db_session, tenant_id, name="NSC Group")
        db_session.commit()
        assert account.name == "NSC Group"
        assert account.parent_account_id is None

    def test_creates_child_account(self, db_session, tenant_id):
        parent = ah.create_grouping_account(db_session, tenant_id, name="NSC Group")
        child = ah.create_grouping_account(
            db_session, tenant_id, name="NSC Taiwan", parent_account_id=parent.id,
        )
        db_session.commit()
        assert child.parent_account_id == parent.id

    def test_rejects_blank_name(self, db_session, tenant_id):
        with pytest.raises(ValueError):
            ah.create_grouping_account(db_session, tenant_id, name="  ")

    def test_rejects_missing_parent(self, db_session, tenant_id):
        with pytest.raises(ValueError):
            ah.create_grouping_account(
                db_session, tenant_id, name="X", parent_account_id=uuid.uuid4(),
            )


class TestSetParentAccount:
    def test_sets_parent(self, db_session, tenant_id):
        parent = make_account(db_session, tenant_id, name="親")
        child = make_account(db_session, tenant_id, name="子")
        db_session.commit()

        ah.set_parent_account(db_session, tenant_id, child, parent.id)
        db_session.commit()
        assert child.parent_account_id == parent.id

    def test_clears_parent(self, db_session, tenant_id):
        parent = make_account(db_session, tenant_id, name="親")
        child = make_account(db_session, tenant_id, name="子", parent_account_id=parent.id)
        db_session.commit()

        ah.set_parent_account(db_session, tenant_id, child, None)
        db_session.commit()
        assert child.parent_account_id is None

    def test_rejects_self_parent(self, db_session, tenant_id):
        account = make_account(db_session, tenant_id)
        db_session.commit()
        with pytest.raises(ValueError):
            ah.set_parent_account(db_session, tenant_id, account, account.id)

    def test_rejects_cycle(self, db_session, tenant_id):
        grandparent = make_account(db_session, tenant_id, name="祖父")
        parent = make_account(db_session, tenant_id, name="親", parent_account_id=grandparent.id)
        db_session.commit()

        with pytest.raises(ValueError):
            ah.set_parent_account(db_session, tenant_id, grandparent, parent.id)


class TestListAccountsTreeOrdered:
    def test_children_follow_parent(self, db_session, tenant_id):
        # 同一スクリプト(日本語)の名前だけを使う — DB照合順序がスクリプト
        # をまたぐと環境依存になりやすいため、木構造そのものの検証に絞る。
        parent = make_account(db_session, tenant_id, name="親会社グループ")
        make_account(db_session, tenant_id, name="台湾子会社", parent_account_id=parent.id)
        make_account(db_session, tenant_id, name="韓国子会社", parent_account_id=parent.id)
        make_account(db_session, tenant_id, name="独立企業")
        db_session.commit()

        ordered = ah.list_accounts_tree_ordered(db_session, tenant_id)
        names = [a.name for a in ordered]
        parent_index = names.index("親会社グループ")
        child_names = {"台湾子会社", "韓国子会社"}
        # 子2件は親の直後に連続して現れる
        assert set(names[parent_index + 1:parent_index + 3]) == child_names
        assert "独立企業" not in names[parent_index + 1:parent_index + 3]


class TestGetFamilyAccountIds:
    def test_includes_self_and_descendants(self, db_session, tenant_id):
        parent = make_account(db_session, tenant_id, name="親")
        child1 = make_account(db_session, tenant_id, name="子1", parent_account_id=parent.id)
        child2 = make_account(db_session, tenant_id, name="子2", parent_account_id=parent.id)
        other = make_account(db_session, tenant_id, name="無関係")
        db_session.commit()

        family = ah.get_family_account_ids(db_session, tenant_id, parent.id)
        assert family == {parent.id, child1.id, child2.id}
        assert other.id not in family


class TestListEngagementsAndLeadsForAccount:
    def test_lists_engagements_for_account(self, db_session, tenant_id):
        from tests.conftest import create_account_and_engagement

        account, engagement = create_account_and_engagement(db_session, tenant_id)
        db_session.commit()

        engagements = ah.list_engagements_for_account(db_session, tenant_id, account.id)
        assert [e.id for e in engagements] == [engagement.id]

    def test_lists_matched_leads_for_account(self, db_session, tenant_id):
        from crm_mvp.models import Lead

        account = make_account(db_session, tenant_id)
        lead = Lead(
            tenant_id=tenant_id, company_name="テスト企業", full_name="山田太郎",
            matched_account_id=account.id, written_by="human:tester",
        )
        db_session.add(lead)
        db_session.commit()

        leads = ah.list_leads_for_account(db_session, tenant_id, account.id)
        assert [l.id for l in leads] == [lead.id]
