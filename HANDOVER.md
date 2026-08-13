# 引き継ぎ書 — Compliance-aware Agentic CRM

作成日: 2026-08-11
引き継ぎ先: Claude Code
現状: プロダクト方向性の合意済み、MVP モデル定義完了、API 層未着手

---

## 0. このドキュメントの読み方

`§3 不変条件` は設計の背骨であり、**実装の都合で変更してはならない**。
迷ったときは §3 に立ち返ること。`§7 残論点` は未決であり、
実装前に人間の判断を仰ぐ。それ以外は裁量で進めてよい。

---

## 1. プロダクト概要

> **「Lead から Contract まで、一つのオブジェクトグラフで貫く
> Compliance-aware Agentic CRM」**

CRM を「案件の状態を記録する箱」ではなく、**顧客との価値共創プロセスを
可観測にする計器盤**として設計する。記録すべきはステージではなく
「顧客側で何が動いたか」であり、これが UI 設計思想の差になる。

### ICP

日本の中堅〜エンタープライズ製造業。
訴求軸は **与信・反社・輸出管理込みの取引管理**。
売り手主導のマスマーケティング的な管理は明示的に目指さない。

### ポジショニング: Companion

既存 CRM（Salesforce / HubSpot）を置き換えず、**隣に置く**。
コンプライアンス・契約レイヤーを先に取り、既存 CRM には
「コンプラ・ステータス」1フィールドだけ書き戻す。
最小の侵襲で最大の可視性を生む接点を狙う。

### 競合ベンチマークからの判断

| 競合 | 構造的弱点 |
|---|---|
| Salesforce | 実装コストとデータモデルの重さ。CPQ/CLM は別 SKU |
| HubSpot | 2006年のインバウンド起点データモデルが不変。AI は上に乗っただけ |
| Attio | AI をデータモデル内に組み込む思想は正しい。ただし契約・コンプラは持たない |

**空白地帯**: どのプレイヤーも Lead〜商談までは厚いが、
契約・与信・コンプライアンスまで一つのオブジェクトグラフで貫いていない。

---

## 2. アーキテクチャ

### スタック

- FastAPI / Python 3.12+
- SQLAlchemy 2.0（宣言的 Mapped スタイル）
- PostgreSQL（JSONB 必須、pgvector は将来の検索用に予約）
- LLM: ハイブリッドルーティング（ローカル Qwen2.5-14B で分類・抽出、
  Claude で複雑推論）。既存 AI_TM の構成を踏襲する
- 非同期処理: outbox パターン + ワーカー

### 既存資産との接続

| 資産 | 役割 |
|---|---|
| AI_TM | 輸出管理該非判定。`ScreeningPort` の一 Adapter として接続 |
| Mini Global ERP | Product / PriceBook のマスター。CRM は参照に徹する |
| WorkPilot | ブラウザオーバーレイによる入力チャネル |

### パッケージ構成（実装済み）

```
crm_mvp/
  enums.py                    製品標準の列挙型
  base.py                     宣言的ベース、Provenance ミックスイン
  models/
    party.py                  Account / Contact / ComplianceStatus
    engagement.py             Engagement / StageTransition / PipelineSnapshot
    qualification.py          QualificationSlot
    buying_center.py          GraphNode / GraphEdge / EngagementRole
    gate.py                   GatePolicy / GateEvaluation / Waiver
    ingestion.py              IngestionSource / ExtractionProposal
                              / FieldAutonomyPolicy
  schemas/extraction.py       LLM 抽出契約、criterion 別スキーマ、獲得の一手
  services/
    gate_engine.py            required - known = missing、グラフ演算
    extraction_pipeline.py    取り込み → 提案 → 適用
    seed_policies.py          製造業テンプレート、初期自動化設定
```

全16テーブル。インポートとゲートエンジンの疎通確認済み。

---

## 3. 不変条件（変更禁止）

実装の都合でここを崩すと、プロダクトの差別化が消える。

### 3.1 AI は業務テーブルに直接書かない

すべての AI 由来の書き込みは `ExtractionProposal` を経由する。
これにより監査可能性・取り消し可能性・自己調整の3つが同時に成立する。
「パフォーマンスのため直接書く」は認めない。

### 3.2 `VERIFIED` は人しか付与できない

`confidence_for_ai_write()` の上限は `CORROBORATED`。
`NEVER_AI_FIELDS` に列挙されたフィールドは AI 書き込み禁止。
ここを緩めると、AI が埋めた数字で経営が意思決定する事故が起きる。

### 3.3 役割と態度は直交する

`buying_center_role`（決裁者・チャンピオン等）と `stance`（支持・反対）は
別カラムで持つ。決裁者かつ反対者は普通に存在する。
1フィールドに統合してはならない。

### 3.4 `approves` は `reports_to` と別に持つ

日本の稟議ルートは組織階層と一致せず、他部門の合議先が挟まる。
この2つを同一視した瞬間に、稟議リードタイム逆算が機能しなくなる。

### 3.5 入口は緩く、出口は固い

案件化ゲートを厳しくすると担当者はリードを登録しなくなり、
パイプラインが痩せるだけで統制にならない。
`BLOCK` を使ってよいのは **Artifact Gate（見積・契約書の発行）だけ**。

### 3.6 不足は「チェックリスト」ではなく「次の一手」1件で返す

`GateResult.next_best_action()` は常に1件のみ返す。
複数返す実装に変えてはならない。10件出すと担当者は全件を無視する。

### 3.7 証跡は参照で持つ

反社チェックの記事本文、信用調査レポート等は**一切コピーしない**。
`evidence_uri` + `evidence_hash` のみ。
ストレージ・外部サービス利用規約の両面で必須。

### 3.8 `evidence_quote` なき抽出は破棄する

`ExtractedClaim` のバリデータで強制済み。
根拠を示せない提案は、そもそも提案ではない。

### 3.9 入力口は1つ

担当者が触る入力先は `IngestionSource` のみ。
フィールド別入力フォームを追加してはならない。

### 3.10 ERP / 既存 CRM との所有権境界

| 領域 | System of Record |
|---|---|
| Account / Contact / 商談ステージ・金額 | 既存 CRM（読み取り＋ミラー） |
| Party Role / 需要者・仕向地 | **自社** |
| ComplianceStatus | **自社** |
| Product / PriceBook | ERP（参照） |
| Quote 承認 / Contract | **自社** |

CRM に独自の商品マスターを持たせるのは典型的な失敗パターン。

---

## 4. 人の入力と AI の分担

境界は「可逆性」と「誰の判断か」で引く。

| 段 | 対象 | 確認 |
|---|---|---|
| AI が直接書く | 参加者同定、接触履歴、日時、言及トピック | なし |
| AI が提案 → 人が承認 | 案件評価値、稟議ルート、態度、クローズ日候補 | 1タップ |
| 人しか書けない | `verified` 昇格、ステージ変更、金額確約、例外承認 | — |

### 割合は設定値ではなく実績の関数

`FieldAutonomyPolicy` がフィールド別の承認率を実測し、
閾値（既定 0.90 / 最低30サンプル）超で自動適用に昇格、
下回れば確認モードへ降格する。人間が最初に正しい割合を当てる必要はない。

`recompute_accept_rate()` は自動適用後の手動取り消しを不承認として扱う。
これを外すと承認率が実態より高く出る。

### 抽出は毎回全項目やらない

`build_targets()` がゲート評価の `missing` から抽出対象スキーマを
動的生成する。トークン量・幻覚・提案の関連性が同時に改善する。
AI_TM の `required_params - known_params = missing` と同じ骨格。

---

## 5. 未実装（この順で着手）

### Phase 1 — 基盤

1. Alembic マイグレーション初期化、16テーブルの生成
2. テナント分離の方式決定と適用（→ §7.4）
3. `pytest` 基盤 + ゲートエンジンのユニットテスト
   （`path_to_decider` / `shortest_intro_path` / `is_single_threaded` /
   `derive_close_date` は既にロジック分離済み、テスト容易）
4. `seed_policies.py` の投入コマンド

### Phase 2 — 取り込みパイプライン

5. `POST /sources` — トランスクリプト・メモ・メールの受け口
6. STT ワーカー（録画 URI → transcript）。外部 STT は Port 化する
7. 話者同定: 参加者 → `Contact` / `GraphNode` のマッピング
8. 抽出ワーカー: `build_targets` → LLM 呼び出し → `ExtractionProposal` 生成
9. `route_proposals` による自動適用／確認待ちの振り分け
10. `POST /proposals/{id}/accept|reject` と、却下時の `corrected_value` 収集

### Phase 3 — ゲートと支援

11. `GET /engagements/{id}/gate` — 評価結果と次の一手
12. `POST /engagements/{id}/stage` — 遷移時のゲート適用と `StageTransition` 記録
13. `Waiver` 発行フロー
14. 稟議リードタイム逆算による `derived_close_date` の自動更新

### Phase 4 — 可視化

15. `GET /engagements/{id}/graph` — 相関図データ（ノード・エッジ・レイヤー）
16. フロント: Cytoscape.js または React Flow + **dagre 階層レイアウト**
    （力学モデルは使わない。毎回配置が変わり意味を読み取れないため）
17. サーバー側 Graphviz DOT → SVG 出力（提案書・レビュー資料への貼付用）
18. `PipelineSnapshot` の日次バッチ

### Phase 5 — 外部連携

19. `ScreeningPort` の Protocol 定義と Adapter 実装
    （AI_TM / 反社チェック / 与信）
20. 非同期 submit → webhook callback、冪等キー
    `hash(subject_normalized, check_type, policy_version)`
21. `subscribe` によるモニタリング受信と、
    **制裁リスト更新時の遡及再評価**（進行中商談・有効契約の逆引き）
22. 既存 CRM への `compliance_status` 1フィールド書き戻し

---

## 6. スコープ外（作らない判断）

明示的に作らないと決めたもの。要望が来ても §1 のポジショニングに戻ること。

- リードスコアリングの精緻化（マーケ寄り、ICP に対して価値が薄い）
- メールシーケンス / MA 機能（既存 CRM の領域、Companion の越境）
- 独自の商品マスター・見積計算エンジン（ERP 参照に徹する）
- 顧客ごとのゲート定義 UI（製品側テンプレート＋差分パラメータのみ）
- スクリーニング判定ロジックそのもの（外部に委譲）

---

## 7. 残論点（実装前に人間の判断が必要）

### 7.1 `decays_at` の既定値 — 決定済み(2026-08-13)

criterion ごとに別基準を採用: `economic_buyer` / `champion` は人事異動基準
（次の4月・10月）、`budget` は予算期基準（次の決算期末、既定3月末）、
その他は固定日数（`timing` 90日 / `competition` 120日 / それ以外 180日）。
`crm_mvp/services/decay_policy.py` に実装し、`apply_proposal.py` の
`_apply_qualification_slot` と `/engagements/{id}/slots/{criterion}/verify`
の両方から適用のたびに引き直す。

### 7.2 `verified` の定義 — 決定済み(2026-08-13)

暫定案どおり採用: 顧客文書・発言の添付（`evidence_uri`）を標準、
上長確認（`verification_note`）を代替経路として許容する。
`POST /engagements/{id}/slots/{criterion}/verify` が唯一の昇格経路
（`ExtractionProposal` を経由しない、人のみが呼べるエンドポイント）。
`VerificationMethod` enum、`QualificationSlot.evidence_uri` /
`verification_method` / `verification_note` / `verified_by` /
`verified_at` を追加。

### 7.3 `stance` / `influence` のエクスポート時マスク — 実装済み(2026-08-13)

方針どおりシリアライザ層で既定マスク。`graph_export.build_graph_json` /
`build_graph_dot` に `include_sensitive: bool = False` を追加し、
JSON API・SVG・Jinja 画面(`/ui/graph`)すべてで既定非表示。
明示的に `include_sensitive=true` を渡した場合のみ表示し、Jinja 画面には
「取引先に渡す資料には含めないでください」の警告を表示する。

### 7.4 テナント分離の方式 — 決定済み(2026-08-13): Row-Level Security

全16テーブルに RLS ポリシーを設定（`tenant_id = current_setting('app.current_tenant_id')`）。
**重要な実装上の発見**: ローカル DB ユーザーが superuser だったため
`FORCE ROW LEVEL SECURITY` を付けても RLS が素通りしていた
（superuser は RLS を無条件にバイパスする Postgres の仕様で回避不可）。
非 superuser ロール `crm_app` を新設し(`scripts/provision_app_role.sql`)、
アプリケーション・運用スクリプトはこのロールで接続する。Alembic
マイグレーション(DDL)は引き続き所有者ロールで実行する。
`crm_mvp/api/deps.py` の `get_tenant_scoped_session` がリクエストごとに
`SET LOCAL` 相当(`set_config(..., true)`)でテナント文脈を設定する。
アプリ層の `WHERE tenant_id = ...` フィルタは多層防御として維持。

### 7.5 `evidence_score` の算出式 — 暫定式を実装済み、正式決定は引き続き未定

`crm_mvp/services/snapshot.py` の `compute_evidence_score` に暫定式
（有効な QualificationSlot の証拠強度ランクの平均）を実装。
正式な算出式は未決のままで、差し替え可能な形にしてある。

### 7.6 スナップショットの保持期間 — 決定済み(2026-08-13)

日次90日 → 週次1年 → 月次永年のロールアップを採用。
`crm_mvp/services/snapshot_rollup.py` に実装（集計ではなく間引き —
状態値は合算できないため各期間の最新1件を代表値として残す）。
`scripts/rollup_snapshots.py` で週次実行を想定。

### 7.7 課金モデル

シート課金だと AI 処理コストと利益が逆相関する。
Salesforce が work-unit 課金へ移行しようとしているのは同じ構造。
プロダクト設計には直結しないが、`ExtractionProposal` の件数が
自然な課金メトリクスになりうることは念頭に置く。（未着手）

---

## 8. 用語

| 用語 | 意味 |
|---|---|
| Engagement | Lead〜Contract を貫く単一エンティティ |
| Slot | クオリフィケーションの1評価軸（主張＋根拠＋証拠強度） |
| Gate | ステージ遷移・帳票発行の前提条件。強度4段階 |
| Proposal | AI が提案する単一の書き込み。承認されて初めて反映 |
| Play | 不足情報を獲得するための具体的な一手 |
| Node / Edge | バイヤー相関図の人物と関係 |
| Companion | 既存 CRM を置き換えず隣に置く配置戦略 |

---

## 9. 最初の一手（推奨）

Phase 1 の 1〜3 を通し、`services/gate_engine.py` に対する
テストスイートを先に固めることを勧める。
このファイルがプロダクトの論理的中核であり、
ここが壊れると管理側面も支援側面も同時に壊れる。

`/tmp/smoke.py` 相当の疎通確認は実施済み:
最終交渉ゲートで `paper_process` と `competition` の不足を検出し、
優先度計算により次の一手を1件返すこと、契約発行ゲートで
`verified` 強度の稟議ルート不在により `block` すること、
稟議3階層＋法務レビューからクローズ日を逆算することを確認している。
