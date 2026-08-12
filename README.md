# Compliance-aware Agentic CRM — MVP モデル定義

FastAPI / SQLAlchemy 2.0 / PostgreSQL (JSONB) 前提。

## パッケージ構成

```
crm_mvp/
  enums.py                    製品標準の列挙型（顧客ごとに増やさない）
  base.py                     宣言的ベース、Provenance ミックスイン
  models/
    party.py                  Account / Contact / ComplianceStatus
    engagement.py             Engagement / StageTransition / PipelineSnapshot
    qualification.py          QualificationSlot（主張＋根拠＋証拠強度）
    buying_center.py          GraphNode / GraphEdge / EngagementRole
    gate.py                   GatePolicy / GateEvaluation / Waiver
    ingestion.py              IngestionSource / ExtractionProposal
                              / FieldAutonomyPolicy
  schemas/
    extraction.py             LLM との抽出契約、criterion 別スキーマ、獲得の一手
  services/
    gate_engine.py            required - known = missing、グラフ演算、次の一手
    extraction_pipeline.py    取り込み → 提案 → 適用のオーケストレーション
    seed_policies.py          製造業テンプレートの標準ゲート／初期自動化設定
```

## 人の入力と AI の分担

書き込み権限を3段に分ける。境界は「可逆性」と「誰の判断か」で引く。

| 段 | 対象 | 確認 |
|---|---|---|
| AI が直接書く | 参加者の同定、接触履歴、日時、言及トピック | なし |
| AI が提案し人が承認 | クオリフィケーション値、稟議ルート、態度、クローズ日候補 | 1タップ |
| 人しか書けない | `VERIFIED` への昇格、ステージ変更、金額確約、例外承認 | — |

3段目は `extraction_pipeline.NEVER_AI_FIELDS` で機械的に強制している。
AI が埋めた数字で経営が意思決定する事故を、設計段階で不可能にする。

### 割合は人が決めない

`FieldAutonomyPolicy` がフィールドごとの承認率を実測し、閾値
（既定 0.90／最低30サンプル）を超えたものから自動適用に昇格する。
下回れば自動的に確認モードへ降格する。

つまり「AI にどこまで任せるか」は設定値ではなく**運用実績の関数**になる。
導入初期は確認が多く、精度が出た項目から順に手が離れていく。

### 入力口は1つだけ

「どのフィールドに入れればいいか分からない」への回答は、
**入れる場所を最初から1つにする**こと。担当者は `IngestionSource` に
投げるだけでよい（トランスクリプト、録画URI、メール、自由記述メモ）。
フィールド分けは抽出パイプラインの仕事であって、人の仕事ではない。

### 毎回すべてを抽出させない

`build_targets()` はゲート評価の `missing` から抽出対象スキーマを
動的に生成する。その商談で今まさに不足している項目だけを LLM に渡すため、
トークン量・幻覚・提案の関連性が同時に改善する。

抽出結果は `evidence_quote` が無ければ破棄する（`ExtractedClaim` の
バリデータで強制）。根拠を示せない提案は、そもそも提案ではない。

## 動作確認

```bash
pip install sqlalchemy pydantic
PYTHONPATH=. python -c "
from crm_mvp.models import Account
print(sorted(t.name for t in Account.metadata.sorted_tables))
"
```

## MVP に含めていないもの

Product / PriceBook / Quote / Contract の実体（ERP 参照に徹する）、
外部スクリーニングの Adapter 実装、既存 CRM との双方向同期、
テナント分離、認証。いずれもこの骨格の外側に付く。

## 次に決めるべきこと

- `decays_at` の既定値。人事異動と予算期のどちらを基準に置くか
- `stance` / `influence` のエクスポート時マスク既定動作
- スナップショットの保持期間と、`evidence_score` の算出式
