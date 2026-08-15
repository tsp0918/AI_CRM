"""取引先(Account)の法人階層と、取引先を起点にした商談・リードの横断表示。

2026-08-13 ユーザー要望:
  - 「商談の紐付けとして、取引先をヒエラルキートップに置いたディレクトリ
    構造」— Account.parent_account_id による法人グループのロールアップ
    (例: NSC Taiwan/Korea/Singapore/USA を親アカウント "NSC Group" の下に
    まとめる)
  - 「Leadもアカウントの下の階層に入ってくると、Account-based Marketing
    の戦略を立てる際に組織構造からアタック先を選定しやすくなりそう」—
    Lead.matched_account_id(既存フィールド)を商談化前から手動で設定でき
    るようにし、Account配下にLeadも並べて表示する。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Account, Engagement, Lead


def create_grouping_account(
    session: Session, tenant_id: uuid.UUID, *,
    name: str, parent_account_id: uuid.UUID | None = None,
) -> Account:
    """ERPの取引先データに紐付かない、法人グループをまとめるためだけの
    CRM独自のAccountを作る(例: "NSC Group")。

    C2-4(Account作成時の自動スクリーニングフック)は意図的に適用しない —
    これは取引の相手方ではなく階層をまとめるための箱であり、それ自体が
    輸出管理・与信の対象になることはない。"""
    if not name.strip():
        raise ValueError("取引先名を入力してください")

    if parent_account_id is not None:
        parent = session.get(Account, parent_account_id)
        if parent is None or parent.tenant_id != tenant_id:
            raise ValueError("親取引先が見つかりません")

    account = Account(
        tenant_id=tenant_id, name=name.strip(), parent_account_id=parent_account_id,
    )
    session.add(account)
    session.flush()
    return account


def set_parent_account(
    session: Session, tenant_id: uuid.UUID, account: Account,
    parent_account_id: uuid.UUID | None,
) -> Account:
    """既存Accountの親を設定・変更する。自分自身や自分の子孫を親に指定
    しようとした場合は循環参照になるため拒否する。"""
    if parent_account_id is None:
        account.parent_account_id = None
        session.flush()
        return account

    if parent_account_id == account.id:
        raise ValueError("自分自身を親には設定できません")

    parent = session.get(Account, parent_account_id)
    if parent is None or parent.tenant_id != tenant_id:
        raise ValueError("親取引先が見つかりません")

    descendant_ids = get_family_account_ids(session, tenant_id, account.id)
    if parent_account_id in descendant_ids:
        raise ValueError("子孫にあたる取引先を親には設定できません(循環参照)")

    account.parent_account_id = parent_account_id
    session.flush()
    return account


def list_accounts(session: Session, tenant_id: uuid.UUID) -> list[Account]:
    return session.execute(
        select(Account).where(Account.tenant_id == tenant_id).order_by(Account.name)
    ).scalars().all()


def list_accounts_tree_ordered(session: Session, tenant_id: uuid.UUID) -> list[Account]:
    """親アカウントの直後にその子が続く順序で返す(表示用)。"""
    accounts = list_accounts(session, tenant_id)
    by_parent: dict[uuid.UUID | None, list[Account]] = {}
    for account in accounts:
        by_parent.setdefault(account.parent_account_id, []).append(account)

    ordered: list[Account] = []

    def walk(parent_id: uuid.UUID | None) -> None:
        for account in by_parent.get(parent_id, []):
            ordered.append(account)
            walk(account.id)

    walk(None)
    return ordered


def get_family_account_ids(
    session: Session, tenant_id: uuid.UUID, account_id: uuid.UUID,
) -> set[uuid.UUID]:
    """指定したAccount自身とその子孫すべてのIDを返す(売上ロールアップ用)。"""
    accounts = list_accounts(session, tenant_id)
    children_by_parent: dict[uuid.UUID, list[uuid.UUID]] = {}
    for a in accounts:
        if a.parent_account_id is not None:
            children_by_parent.setdefault(a.parent_account_id, []).append(a.id)

    family = {account_id}
    frontier = [account_id]
    while frontier:
        current = frontier.pop()
        for child_id in children_by_parent.get(current, []):
            if child_id not in family:
                family.add(child_id)
                frontier.append(child_id)
    return family


def list_engagements_for_account(
    session: Session, tenant_id: uuid.UUID, account_id: uuid.UUID,
) -> list[Engagement]:
    """この取引先の商談履歴(親子リンクの有無を問わず account_id で横断)。
    フィードバック(2yr simulationレポート)で見つかった「取引先の商談履歴が
    見えない」ギャップへの対応。"""
    return session.execute(
        select(Engagement).where(
            Engagement.tenant_id == tenant_id, Engagement.account_id == account_id,
        ).order_by(Engagement.created_at.desc())
    ).scalars().all()


def list_leads_for_account(
    session: Session, tenant_id: uuid.UUID, account_id: uuid.UUID,
) -> list[Lead]:
    """この取引先に手動で紐付け済みのリード(ABM: 商談化前から組織構造で
    アタック先を把握できるようにする)。"""
    return session.execute(
        select(Lead).where(
            Lead.tenant_id == tenant_id, Lead.matched_account_id == account_id,
        ).order_by(Lead.created_at.desc())
    ).scalars().all()
