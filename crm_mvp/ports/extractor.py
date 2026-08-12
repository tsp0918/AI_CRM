"""LLM 抽出ポート。

実運用ではローカル Qwen2.5-14B / Claude へのハイブリッドルーティングに
差し替える（HANDOVER.md §2）。この環境には LLM の資格情報が無いため、
本番では動かない NullExtractor のみを既定実装として提供する。

`crm_mvp.schemas.extraction.SYSTEM_PROMPT` が実装時に守るべき契約
（evidence_quote 必須・targets 以外を抽出しない等）を規定している。
"""

from __future__ import annotations

from typing import Protocol

from ..schemas.extraction import ExtractionRequest, ExtractionResult


class ExtractorPort(Protocol):
    def extract(self, request: ExtractionRequest) -> ExtractionResult: ...


class NullExtractor:
    """常に claims=[] を返す。LLM 未接続環境でもパイプラインの疎通を保つ。"""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        return ExtractionResult(claims=[], extractor_version="null-extractor-v0")
