"""社内ユーザー名簿の取り込み・一覧(user_matrix_CRM.csv、2026-08-13)。

email をキーにテナント内でアップサートする(services/erp_materials.py と
同じ考え方)。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import AuthorityLevel
from ..models import User


def upsert_user(
    session: Session, tenant_id: uuid.UUID, *,
    name: str, email: str, function: str, role: str,
    authority: AuthorityLevel | str = AuthorityLevel.NONE,
    sales_group_id: uuid.UUID | None = None, is_active: bool = True,
) -> User:
    email = email.strip().lower()
    user = session.execute(
        select(User).where(User.tenant_id == tenant_id, User.email == email)
    ).scalar_one_or_none()
    if user is None:
        user = User(tenant_id=tenant_id, email=email)
        session.add(user)

    user.name = name.strip()
    user.function = function.strip()
    user.role = role.strip()
    user.authority = AuthorityLevel(authority).value
    user.sales_group_id = sales_group_id
    user.is_active = is_active
    session.flush()
    return user


def list_users(session: Session, tenant_id: uuid.UUID) -> list[User]:
    return session.execute(
        select(User).where(User.tenant_id == tenant_id).order_by(User.function, User.name)
    ).scalars().all()
