"""web/ 配下の全ページで共有する Jinja2Templates インスタンス。

テンプレート本体は crm_mvp/api/templates/ に置く(web/ パッケージ化の
前から変わらない場所)。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from .common import format_slot_value

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)
templates.env.globals["format_slot_value"] = format_slot_value
