"""バイヤー相関図の JSON / Graphviz DOT 出力(HANDOVER.md §5 Phase4, item 15,17)。

力学モデル(spring layout)は使わない。毎回配置が変わり意味を読み取れないため
— dagre 相当の階層レイアウトを Graphviz の dot エンジンで実現する
(HANDOVER.md §5 Phase4-16 のフロント実装方針と同じ理由)。
"""

from __future__ import annotations

import uuid

import graphviz
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import AccessLevel, Confidence, Stance
from ..models import Engagement, EngagementRole, GraphEdge, GraphNode

_STANCE_FILLCOLOR: dict[str, str] = {
    Stance.SUPPORTER: "#c8f7c5",
    Stance.OPPONENT: "#f7c5c5",
    Stance.NEUTRAL: "#f7f2c5",
    Stance.UNKNOWN: "#ffffff",
}


def _node_label(node: GraphNode) -> str:
    if node.contact is not None:
        return node.contact.full_name
    return node.placeholder_label or "(不明)"


def load_graph_data(
    session: Session, tenant_id: uuid.UUID, engagement: Engagement,
) -> tuple[list[GraphNode], list[GraphEdge], dict[uuid.UUID, EngagementRole]]:
    nodes = session.execute(
        select(GraphNode).where(
            GraphNode.tenant_id == tenant_id,
            GraphNode.account_id == engagement.account_id,
        )
    ).scalars().all()
    edges = session.execute(
        select(GraphEdge).where(
            GraphEdge.tenant_id == tenant_id,
            GraphEdge.account_id == engagement.account_id,
        )
    ).scalars().all()
    role_rows = session.execute(
        select(EngagementRole).where(
            EngagementRole.tenant_id == tenant_id,
            EngagementRole.engagement_id == engagement.id,
        )
    ).scalars().all()
    roles = {r.node_id: r for r in role_rows}
    return list(nodes), list(edges), roles


def build_graph_json(
    session: Session, tenant_id: uuid.UUID, engagement: Engagement,
) -> dict:
    nodes, edges, roles = load_graph_data(session, tenant_id, engagement)
    return {
        "nodes": [
            {
                "id": str(n.id),
                "label": _node_label(n),
                "layer": n.seniority_layer,
                "org_unit": n.org_unit,
                "is_placeholder": n.contact_id is None,
                "roles": roles[n.id].roles if n.id in roles else [],
                "stance": (roles[n.id].stance if n.id in roles else Stance.UNKNOWN),
                "influence": roles[n.id].influence if n.id in roles else 3,
                "access_level": (
                    roles[n.id].access_level if n.id in roles else AccessLevel.NONE
                ),
            }
            for n in nodes
        ],
        "edges": [
            {
                "id": str(e.id), "from": str(e.from_node_id), "to": str(e.to_node_id),
                "type": e.edge_type, "sequence": e.sequence,
                "strength": e.strength, "confidence": e.confidence,
            }
            for e in edges
        ],
    }


def build_graph_dot(
    session: Session, tenant_id: uuid.UUID, engagement: Engagement,
) -> graphviz.Digraph:
    nodes, edges, roles = load_graph_data(session, tenant_id, engagement)

    dot = graphviz.Digraph(
        name=f"engagement_{engagement.id}",
        graph_attr={"rankdir": "BT", "splines": "polyline"},
        node_attr={"shape": "box", "style": "filled,rounded", "fontname": "Helvetica"},
        edge_attr={"fontname": "Helvetica", "fontsize": "10"},
    )

    by_layer: dict[int, list[GraphNode]] = {}
    for n in nodes:
        by_layer.setdefault(n.seniority_layer, []).append(n)

    for layer, layer_nodes in by_layer.items():
        with dot.subgraph() as sub:
            sub.attr(rank="same")
            for n in layer_nodes:
                role = roles.get(n.id)
                stance = role.stance if role else Stance.UNKNOWN
                access = role.access_level if role else AccessLevel.NONE
                sub.node(
                    str(n.id),
                    label=_node_label(n),
                    fillcolor=_STANCE_FILLCOLOR.get(stance, "#ffffff"),
                    style="dashed,rounded" if access == AccessLevel.NONE
                    else "filled,rounded",
                )

    for e in edges:
        dot.edge(
            str(e.from_node_id), str(e.to_node_id),
            label=f"{e.edge_type}" + (f" #{e.sequence}" if e.sequence else ""),
            style="bold" if e.confidence == Confidence.VERIFIED else (
                "solid" if e.confidence == Confidence.CORROBORATED else "dashed"
            ),
        )

    return dot
