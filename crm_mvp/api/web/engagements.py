"""案件の新規作成・詳細・ステージ遷移・Waiver発行・VERIFIED昇格(骨格 CRM の中核)。"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...enums import (
    Confidence, ContractStatus, Criterion, EngagementRelationshipType,
    ProposalStatus, QuoteStatus, Stage, VerificationMethod,
)
from ...models import (
    Account, ActionItem, Campaign, Contract, Engagement, EngagementLineItem,
    ExtractionProposal, GraphNode, IngestionSource, Lead, Product,
    QualificationSlot, Quote, SalesGroup, User, Waiver,
)
from ...services.action_items import (
    assign_action_item, complete_action_item, create_manual_action_item,
    dismiss_action_item, list_open_action_items,
)
from ...services.activity_log import load_activity_log
from ...services.confidence_score import compute_confidence_score, score_reasons
from ...services.decay_policy import compute_decays_at
from ...services.engagement_relationships import (
    create_child_engagement, list_child_engagements,
)
from ...services.pricing import add_line_item, list_line_items, remove_line_item
from ...services.sales_groups import list_sales_groups_tree_ordered
from ...services.quoting import (
    create_contract, create_quote_from_engagement, list_contract_line_items,
    list_contracts, list_quote_line_items, list_quotes, update_contract_status,
    update_quote_status,
)
from ...services.stage_transitions import (
    STAGE_ORDER, apply_stage_transition, evaluate_stage_gate, load_gate_context,
    next_stage,
)
from .common import CRITERION_LABELS, base_context, redirect_with_flash
from .leads import SOURCE_CHANNEL_LABELS, TOUCH_CHANNEL_LABELS
from .session import UiSession, get_ui_db_session, require_ui_session
from .templates import templates

router = APIRouter(tags=["web"])


# --- 新規案件作成 ------------------------------------------------------------

@router.get("/ui/engagements/new", response_class=HTMLResponse)
def engagement_new_form(
    request: Request,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    context = base_context(session, ui_session, active_nav="new", request=request)
    context.update({"sales_groups": list_sales_groups_tree_ordered(session, ui_session.tenant_id)})
    return templates.TemplateResponse(request, "engagement_new.html", context)


@router.post("/ui/engagements/new")
def engagement_new_submit(
    account_name: str = Form(...),
    engagement_name: str = Form(...),
    sales_group_id: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    if not account_name.strip() or not engagement_name.strip():
        return redirect_with_flash(
            "/ui/engagements/new", "取引先名と案件名を入力してください", "error",
        )

    account = Account(tenant_id=ui_session.tenant_id, name=account_name.strip())
    session.add(account)
    session.flush()

    engagement = Engagement(
        tenant_id=ui_session.tenant_id, account_id=account.id,
        name=engagement_name.strip(), stage=Stage.LEAD,
        sales_group_id=uuid.UUID(sales_group_id) if sales_group_id.strip() else None,
    )
    session.add(engagement)
    session.commit()

    return redirect_with_flash(
        f"/ui/engagements/{engagement.id}", "案件を作成しました。情報を投入して育てていきましょう。",
    )


# --- 案件詳細 ----------------------------------------------------------------

def _get_engagement_or_404(
    session: Session, ui_session: UiSession, engagement_id: uuid.UUID,
) -> Engagement:
    engagement = session.get(Engagement, engagement_id)
    if engagement is None or engagement.tenant_id != ui_session.tenant_id:
        raise HTTPException(status_code=404, detail="engagement not found")
    return engagement


@router.get("/ui/engagements/{engagement_id}", response_class=HTMLResponse)
def engagement_detail(
    request: Request,
    engagement_id: uuid.UUID,
    flash: str | None = None,
    flash_type: str = "info",
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    engagement = _get_engagement_or_404(session, ui_session, engagement_id)
    account = session.get(Account, engagement.account_id)

    target_stage = next_stage(engagement.stage)
    policy, gate, next_best_action = (None, None, None)
    if target_stage is not None:
        policy, gate = evaluate_stage_gate(
            session, ui_session.tenant_id, engagement, target_stage,
        )
        next_best_action = gate.next_best_action()

    slot_rows = session.execute(
        select(QualificationSlot).where(
            QualificationSlot.tenant_id == ui_session.tenant_id,
            QualificationSlot.engagement_id == engagement.id,
        )
    ).scalars().all()
    slots_by_criterion = {s.criterion: s for s in slot_rows}
    criteria = [
        {"criterion": c, "slot": slots_by_criterion.get(c.value)}
        for c in Criterion
    ]

    pending_proposals = session.execute(
        select(ExtractionProposal).where(
            ExtractionProposal.tenant_id == ui_session.tenant_id,
            ExtractionProposal.engagement_id == engagement.id,
            ExtractionProposal.status == ProposalStatus.PENDING,
        ).order_by(ExtractionProposal.created_at.desc())
    ).scalars().all()

    sources = session.execute(
        select(IngestionSource).where(
            IngestionSource.tenant_id == ui_session.tenant_id,
            IngestionSource.engagement_id == engagement.id,
        ).order_by(IngestionSource.created_at.desc()).limit(5)
    ).scalars().all()

    node_count = session.execute(
        select(func.count()).select_from(GraphNode).where(
            GraphNode.tenant_id == ui_session.tenant_id,
            GraphNode.account_id == engagement.account_id,
        )
    ).scalar_one()

    waivers = session.execute(
        select(Waiver).where(
            Waiver.tenant_id == ui_session.tenant_id,
            Waiver.engagement_id == engagement.id,
        ).order_by(Waiver.approved_at.desc())
    ).scalars().all()
    matching_waiver = next(
        (w for w in waivers if policy and w.policy_id == policy.id), None,
    )

    recent_activity = load_activity_log(session, ui_session.tenant_id, engagement)[:5]

    gate_ctx = load_gate_context(session, ui_session.tenant_id, engagement)
    score = compute_confidence_score(
        gate_ctx["slots"], gate_ctx["nodes"], gate_ctx["edges"], gate_ctx["roles"],
    )
    reasons = score_reasons(score, CRITERION_LABELS)

    open_actions = list_open_action_items(session, ui_session.tenant_id, engagement.id)

    originating_lead = None
    originating_campaign = None
    if engagement.originating_lead_id:
        originating_lead = session.get(Lead, engagement.originating_lead_id)
        if originating_lead and originating_lead.source_campaign_id:
            originating_campaign = session.get(Campaign, originating_lead.source_campaign_id)

    line_items = list_line_items(session, ui_session.tenant_id, engagement.id)
    available_products = session.execute(
        select(Product).where(
            Product.tenant_id == ui_session.tenant_id, Product.is_active.is_(True),
        ).order_by(Product.name)
    ).scalars().all()

    quotes = list_quotes(session, ui_session.tenant_id, engagement.id)
    quote_line_items = {
        q.id: list_quote_line_items(session, ui_session.tenant_id, q.id) for q in quotes
    }
    acceptable_quotes = [q for q in quotes if q.status == QuoteStatus.ACCEPTED]

    contracts = list_contracts(session, ui_session.tenant_id, engagement.id)
    contract_line_items = {
        c.id: list_contract_line_items(session, ui_session.tenant_id, c.id) for c in contracts
    }

    parent_engagement = None
    parent_account = None
    if engagement.parent_engagement_id:
        parent_engagement = session.get(Engagement, engagement.parent_engagement_id)
        if parent_engagement:
            parent_account = session.get(Account, parent_engagement.account_id)
    child_engagements = list_child_engagements(session, ui_session.tenant_id, engagement.id)
    sales_groups = list_sales_groups_tree_ordered(session, ui_session.tenant_id)
    current_sales_group = (
        session.get(SalesGroup, engagement.sales_group_id) if engagement.sales_group_id else None
    )
    current_owner_user = (
        session.get(User, engagement.owner_user_id) if engagement.owner_user_id else None
    )

    context = base_context(
        session, ui_session, active_nav="dashboard", request=request, flash=flash, flash_type=flash_type,
    )
    context.update({
        "engagement": engagement, "account": account,
        "target_stage": target_stage, "policy": policy, "gate": gate,
        "next_best_action": next_best_action,
        "criteria": criteria, "pending_proposals": pending_proposals,
        "sources": sources, "node_count": node_count, "waivers": waivers,
        "matching_waiver": matching_waiver,
        "stage_order": STAGE_ORDER,
        "verification_methods": list(VerificationMethod),
        "recent_activity": recent_activity,
        "score": score, "score_reasons": reasons,
        "open_actions": open_actions,
        "originating_lead": originating_lead, "originating_campaign": originating_campaign,
        "source_channel_labels": SOURCE_CHANNEL_LABELS,
        "touch_channel_labels": TOUCH_CHANNEL_LABELS,
        "line_items": line_items, "available_products": available_products,
        "quotes": quotes, "quote_line_items": quote_line_items,
        "acceptable_quotes": acceptable_quotes,
        "contracts": contracts, "contract_line_items": contract_line_items,
        "quote_status_values": list(QuoteStatus),
        "contract_status_values": list(ContractStatus),
        "parent_engagement": parent_engagement, "parent_account": parent_account,
        "child_engagements": child_engagements,
        "relationship_type_values": list(EngagementRelationshipType),
        "sales_groups": sales_groups, "current_sales_group": current_sales_group,
        "current_owner_user": current_owner_user,
    })
    return templates.TemplateResponse(request, "engagement_detail.html", context)


# --- 活動ログ -----------------------------------------------------------------

@router.get("/ui/engagements/{engagement_id}/activity", response_class=HTMLResponse)
def activity_log_page(
    request: Request,
    engagement_id: uuid.UUID,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> HTMLResponse:
    engagement = _get_engagement_or_404(session, ui_session, engagement_id)
    account = session.get(Account, engagement.account_id)
    activity = load_activity_log(session, ui_session.tenant_id, engagement)

    context = base_context(session, ui_session, active_nav="dashboard", request=request)
    context.update({
        "engagement": engagement, "account": account, "activity": activity,
    })
    return templates.TemplateResponse(request, "activity_log.html", context)


# --- ステージ遷移 -------------------------------------------------------------

@router.post("/ui/engagements/{engagement_id}/stage")
def transition_stage_ui(
    engagement_id: uuid.UUID,
    to_stage: str = Form(...),
    waiver_id: str = Form(""),
    lost_reason: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    engagement = _get_engagement_or_404(session, ui_session, engagement_id)
    wid = uuid.UUID(waiver_id) if waiver_id else None

    if to_stage == Stage.CLOSED_LOST.value and not lost_reason.strip():
        return redirect_with_flash(
            f"/ui/engagements/{engagement_id}", "失注理由を入力してください", "error",
        )

    try:
        outcome = apply_stage_transition(
            session, ui_session.tenant_id, engagement, Stage(to_stage),
            waiver_id=wid, actor=f"human:{ui_session.actor_id}",
        )
    except ValueError as exc:
        session.rollback()
        return redirect_with_flash(
            f"/ui/engagements/{engagement_id}", str(exc), "error",
        )

    if not outcome.allowed:
        session.rollback()
        return redirect_with_flash(
            f"/ui/engagements/{engagement_id}",
            "ゲートがこの遷移をブロックしています。例外承認を発行してください。", "error",
        )

    if to_stage == Stage.CLOSED_LOST.value:
        engagement.lost_reason = lost_reason.strip()

    session.commit()
    return redirect_with_flash(
        f"/ui/engagements/{engagement_id}",
        f"ステージを「{to_stage}」に更新しました",
    )


# --- Waiver 発行 --------------------------------------------------------------

@router.post("/ui/engagements/{engagement_id}/waivers")
def create_waiver_ui(
    engagement_id: uuid.UUID,
    policy_id: str = Form(...),
    reason: str = Form(...),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    engagement = _get_engagement_or_404(session, ui_session, engagement_id)
    if not reason.strip():
        return redirect_with_flash(
            f"/ui/engagements/{engagement_id}", "例外承認の理由を入力してください", "error",
        )

    waiver = Waiver(
        tenant_id=ui_session.tenant_id, engagement_id=engagement.id,
        policy_id=uuid.UUID(policy_id), approved_by=ui_session.actor_id,
        reason=reason.strip(), approved_at=datetime.now(timezone.utc),
        written_by=f"human:{ui_session.actor_id}",
    )
    session.add(waiver)
    session.commit()

    return redirect_with_flash(
        f"/ui/engagements/{engagement_id}", "例外承認を発行しました。ステージ遷移を再度お試しください。",
    )


# --- VERIFIED 昇格 ------------------------------------------------------------

@router.post("/ui/engagements/{engagement_id}/slots/{criterion}/verify")
def verify_slot_ui(
    engagement_id: uuid.UUID,
    criterion: Criterion,
    method: VerificationMethod = Form(...),
    evidence_uri: str = Form(""),
    note: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    engagement = _get_engagement_or_404(session, ui_session, engagement_id)

    if method == VerificationMethod.CUSTOMER_DOCUMENT and not evidence_uri.strip():
        return redirect_with_flash(
            f"/ui/engagements/{engagement_id}",
            "customer_document 方式には証跡の参照(URI)が必須です", "error",
        )
    if method == VerificationMethod.MANAGER_CONFIRMATION and not note.strip():
        return redirect_with_flash(
            f"/ui/engagements/{engagement_id}",
            "manager_confirmation 方式には確認内容の記録が必須です", "error",
        )

    slot = session.execute(
        select(QualificationSlot).where(
            QualificationSlot.tenant_id == ui_session.tenant_id,
            QualificationSlot.engagement_id == engagement.id,
            QualificationSlot.criterion == criterion,
        )
    ).scalar_one_or_none()
    if slot is None:
        return redirect_with_flash(
            f"/ui/engagements/{engagement_id}",
            "まだ値が無い項目は検証できません。先に情報を投入してください。", "error",
        )

    now = datetime.now(timezone.utc)
    slot.confidence = Confidence.VERIFIED
    slot.evidence_uri = evidence_uri.strip() or None
    slot.verification_method = method
    slot.verification_note = note.strip() or None
    slot.verified_by = ui_session.actor_id
    slot.verified_at = now
    slot.decays_at = compute_decays_at(criterion, now)
    slot.written_by = f"human:{ui_session.actor_id}"
    session.commit()

    return redirect_with_flash(
        f"/ui/engagements/{engagement_id}", f"{criterion.value} を検証済みにしました",
    )


# --- 次の一手のタスク化 --------------------------------------------------------

def _parse_due_at(raw: str) -> datetime | None:
    if not raw.strip():
        return None
    return datetime.combine(date.fromisoformat(raw.strip()), datetime.min.time(), tzinfo=timezone.utc)


@router.post("/ui/engagements/{engagement_id}/actions")
def assign_next_best_action_ui(
    engagement_id: uuid.UUID,
    assigned_to: str = Form(...),
    due_at: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    """今この瞬間のゲート評価から next_best_action を再計算し、それを
    タスクとしてアサインする。クライアントが送ってきた reason/play を
    信用せず、常にサーバー側で新鮮に再計算する。"""
    engagement = _get_engagement_or_404(session, ui_session, engagement_id)
    if not assigned_to.strip():
        return redirect_with_flash(
            f"/ui/engagements/{engagement_id}", "担当者名を入力してください", "error",
        )

    target_stage = next_stage(engagement.stage)
    if target_stage is None:
        return redirect_with_flash(
            f"/ui/engagements/{engagement_id}", "これ以上進むステージがありません", "error",
        )
    _, gate = evaluate_stage_gate(session, ui_session.tenant_id, engagement, target_stage)
    action = gate.next_best_action()
    if action is None:
        return redirect_with_flash(
            f"/ui/engagements/{engagement_id}", "現時点で提示できる次の一手がありません", "error",
        )

    assign_action_item(
        session, ui_session.tenant_id, engagement.id, action,
        assigned_to=assigned_to.strip(), assigned_by=f"human:{ui_session.actor_id}",
        due_at=_parse_due_at(due_at),
    )
    session.commit()
    return redirect_with_flash(
        f"/ui/engagements/{engagement_id}", f"{assigned_to.strip()} に次の一手をアサインしました",
    )


@router.post("/ui/engagements/{engagement_id}/actions/manual")
def create_manual_action_item_ui(
    engagement_id: uuid.UUID,
    assigned_to: str = Form(...),
    task: str = Form(...),
    due_at: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    """1on1で決めた自由なタスクを追加する(ゲート判定を経由しない)。"""
    _get_engagement_or_404(session, ui_session, engagement_id)
    if not assigned_to.strip() or not task.strip():
        return redirect_with_flash(
            f"/ui/engagements/{engagement_id}", "担当者名とタスク内容を入力してください", "error",
        )

    create_manual_action_item(
        session, ui_session.tenant_id, engagement_id,
        assigned_to=assigned_to.strip(), task=task.strip(),
        assigned_by=f"human:{ui_session.actor_id}", due_at=_parse_due_at(due_at),
    )
    session.commit()
    return redirect_with_flash(f"/ui/engagements/{engagement_id}", "タスクを追加しました")


def _get_action_or_404(
    session: Session, ui_session: UiSession, engagement_id: uuid.UUID, action_id: uuid.UUID,
) -> ActionItem:
    action = session.execute(
        select(ActionItem).where(
            ActionItem.tenant_id == ui_session.tenant_id,
            ActionItem.id == action_id, ActionItem.engagement_id == engagement_id,
        )
    ).scalar_one_or_none()
    if action is None:
        raise HTTPException(status_code=404, detail="action item not found")
    return action


@router.post("/ui/engagements/{engagement_id}/actions/{action_id}/complete")
def complete_action_item_ui(
    engagement_id: uuid.UUID,
    action_id: uuid.UUID,
    note: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    action = _get_action_or_404(session, ui_session, engagement_id, action_id)
    complete_action_item(action, note=note.strip() or None)
    session.commit()
    return redirect_with_flash(f"/ui/engagements/{engagement_id}", "アクションを完了にしました")


@router.post("/ui/engagements/{engagement_id}/actions/{action_id}/dismiss")
def dismiss_action_item_ui(
    engagement_id: uuid.UUID,
    action_id: uuid.UUID,
    note: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    action = _get_action_or_404(session, ui_session, engagement_id, action_id)
    dismiss_action_item(action, note=note.strip() or None)
    session.commit()
    return redirect_with_flash(f"/ui/engagements/{engagement_id}", "アクションを却下にしました")


# --- 金額の手動編集(商品構成が無い案件のみ) -----------------------------------

@router.post("/ui/engagements/{engagement_id}/amount")
def update_amount_ui(
    engagement_id: uuid.UUID,
    amount: str = Form(""),
    currency: str = Form("JPY"),
    expected_close_date: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    engagement = _get_engagement_or_404(session, ui_session, engagement_id)
    if list_line_items(session, ui_session.tenant_id, engagement.id):
        return redirect_with_flash(
            f"/ui/engagements/{engagement_id}",
            "商品構成がある案件の金額は明細から自動計算されます", "error",
        )

    if amount.strip():
        try:
            engagement.amount = Decimal(amount.strip())
        except Exception:
            return redirect_with_flash(
                f"/ui/engagements/{engagement_id}", "金額は数値で入力してください", "error",
            )
    else:
        engagement.amount = None
    engagement.currency = currency.strip() or "JPY"
    engagement.expected_close_date = (
        date.fromisoformat(expected_close_date.strip()) if expected_close_date.strip() else None
    )
    session.commit()
    return redirect_with_flash(f"/ui/engagements/{engagement_id}", "金額を更新しました")


# --- 商品構成(EngagementLineItem) ---------------------------------------------

@router.post("/ui/engagements/{engagement_id}/line-items")
def add_line_item_ui(
    engagement_id: uuid.UUID,
    product_id: str = Form(...),
    quantity: str = Form("1"),
    discount_rate: str = Form("0"),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    engagement = _get_engagement_or_404(session, ui_session, engagement_id)
    product = session.get(Product, uuid.UUID(product_id))
    if product is None or product.tenant_id != ui_session.tenant_id:
        raise HTTPException(status_code=404, detail="product not found")

    try:
        add_line_item(
            session, ui_session.tenant_id, engagement, product=product,
            quantity=int(quantity), discount_rate=Decimal(discount_rate),
        )
    except (ValueError, ArithmeticError) as exc:
        session.rollback()
        return redirect_with_flash(f"/ui/engagements/{engagement_id}", str(exc), "error")

    session.commit()
    return redirect_with_flash(f"/ui/engagements/{engagement_id}", f"{product.name} を追加しました")


@router.post("/ui/engagements/{engagement_id}/line-items/{line_item_id}/remove")
def remove_line_item_ui(
    engagement_id: uuid.UUID,
    line_item_id: uuid.UUID,
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    engagement = _get_engagement_or_404(session, ui_session, engagement_id)
    item = session.get(EngagementLineItem, line_item_id)
    if item is None or item.tenant_id != ui_session.tenant_id or item.engagement_id != engagement.id:
        raise HTTPException(status_code=404, detail="line item not found")

    remove_line_item(session, ui_session.tenant_id, engagement, item)
    session.commit()
    return redirect_with_flash(f"/ui/engagements/{engagement_id}", "商品を削除しました")


# --- 見積もり -------------------------------------------------------------------

@router.post("/ui/engagements/{engagement_id}/quotes")
def create_quote_ui(
    engagement_id: uuid.UUID,
    valid_until: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    engagement = _get_engagement_or_404(session, ui_session, engagement_id)
    try:
        quote = create_quote_from_engagement(
            session, ui_session.tenant_id, engagement,
            valid_until=date.fromisoformat(valid_until.strip()) if valid_until.strip() else None,
            actor=f"human:{ui_session.actor_id}",
        )
    except ValueError as exc:
        session.rollback()
        return redirect_with_flash(f"/ui/engagements/{engagement_id}", str(exc), "error")

    session.commit()
    return redirect_with_flash(
        f"/ui/engagements/{engagement_id}", f"見積もり {quote.quote_number} を作成しました",
    )


def _get_quote_or_404(
    session: Session, ui_session: UiSession, engagement_id: uuid.UUID, quote_id: uuid.UUID,
) -> Quote:
    quote = session.get(Quote, quote_id)
    if quote is None or quote.tenant_id != ui_session.tenant_id or quote.engagement_id != engagement_id:
        raise HTTPException(status_code=404, detail="quote not found")
    return quote


@router.post("/ui/engagements/{engagement_id}/quotes/{quote_id}/status")
def update_quote_status_ui(
    engagement_id: uuid.UUID,
    quote_id: uuid.UUID,
    status: str = Form(...),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    quote = _get_quote_or_404(session, ui_session, engagement_id, quote_id)
    update_quote_status(quote, QuoteStatus(status))
    session.commit()
    return redirect_with_flash(f"/ui/engagements/{engagement_id}", f"見積もりを「{status}」にしました")


# --- 契約 ---------------------------------------------------------------------

@router.post("/ui/engagements/{engagement_id}/contracts")
def create_contract_ui(
    engagement_id: uuid.UUID,
    quote_id: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    engagement = _get_engagement_or_404(session, ui_session, engagement_id)
    quote = None
    if quote_id.strip():
        quote = _get_quote_or_404(session, ui_session, engagement_id, uuid.UUID(quote_id))

    try:
        contract = create_contract(
            session, ui_session.tenant_id, engagement, quote=quote,
            start_date=date.fromisoformat(start_date.strip()) if start_date.strip() else None,
            end_date=date.fromisoformat(end_date.strip()) if end_date.strip() else None,
            actor=f"human:{ui_session.actor_id}",
        )
    except ValueError as exc:
        session.rollback()
        return redirect_with_flash(f"/ui/engagements/{engagement_id}", str(exc), "error")

    session.commit()
    return redirect_with_flash(
        f"/ui/engagements/{engagement_id}", f"契約 {contract.contract_number} を発行しました",
    )


@router.post("/ui/engagements/{engagement_id}/contracts/{contract_id}/status")
def update_contract_status_ui(
    engagement_id: uuid.UUID,
    contract_id: uuid.UUID,
    status: str = Form(...),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    contract = session.get(Contract, contract_id)
    if (
        contract is None or contract.tenant_id != ui_session.tenant_id
        or contract.engagement_id != engagement_id
    ):
        raise HTTPException(status_code=404, detail="contract not found")

    update_contract_status(contract, ContractStatus(status))
    session.commit()
    return redirect_with_flash(f"/ui/engagements/{engagement_id}", f"契約を「{status}」にしました")


# --- 継続/Upsell/Cross-sell(親商談との紐付け) ----------------------------------

@router.post("/ui/engagements/{engagement_id}/child-engagements")
def create_child_engagement_ui(
    engagement_id: uuid.UUID,
    name: str = Form(...),
    relationship_type: str = Form(...),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    parent = _get_engagement_or_404(session, ui_session, engagement_id)
    try:
        child = create_child_engagement(
            session, ui_session.tenant_id, parent,
            relationship_type=EngagementRelationshipType(relationship_type), name=name,
        )
    except ValueError as exc:
        session.rollback()
        return redirect_with_flash(f"/ui/engagements/{engagement_id}", str(exc), "error")

    session.commit()
    return redirect_with_flash(
        f"/ui/engagements/{child.id}", f"商談「{child.name}」を作成しました",
    )


# --- セールスグループ(売上レポート用の営業組織タグ) ---------------------------

@router.post("/ui/engagements/{engagement_id}/sales-group")
def update_sales_group_ui(
    engagement_id: uuid.UUID,
    sales_group_id: str = Form(""),
    ui_session: UiSession = Depends(require_ui_session),
    session: Session = Depends(get_ui_db_session),
) -> RedirectResponse:
    engagement = _get_engagement_or_404(session, ui_session, engagement_id)
    if sales_group_id.strip():
        group = session.get(SalesGroup, uuid.UUID(sales_group_id))
        if group is None or group.tenant_id != ui_session.tenant_id:
            return redirect_with_flash(
                f"/ui/engagements/{engagement_id}", "セールスグループが見つかりません", "error",
            )
        engagement.sales_group_id = group.id
    else:
        engagement.sales_group_id = None
    session.commit()
    return redirect_with_flash(f"/ui/engagements/{engagement_id}", "セールスグループを更新しました")
