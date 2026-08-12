"""音声認識(STT)ポート。

録画 URI から transcript を生成する処理（HANDOVER.md §5 Phase2-6）は
外部 STT サービスとの接続が要るためこの環境では未接続。
呼び出し側は NotImplementedError を捕捉し、
「録画は現状 transcript を直接投入してください」と案内すること。
"""

from __future__ import annotations

from typing import Protocol


class STTPort(Protocol):
    def transcribe(self, recording_uri: str) -> str: ...


class UnavailableSTT:
    def transcribe(self, recording_uri: str) -> str:
        raise NotImplementedError(
            "STT is not connected in this environment. "
            "Submit a transcript via IngestionSource.raw_text instead of "
            "a recording URI."
        )
