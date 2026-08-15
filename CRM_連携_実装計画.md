# CRM連携(ERP / AI_TM)実装計画

文書ID: PLAN-CRM-001
バージョン: 1.0
作成日: 2026-08-15
位置づけ: `CRM_連携引き継ぎ書.md`(以下「引き継ぎ書」)の内容を、**現在の`crm_mvp`コードベースの実態**に照らして精査し、実装順序・既存コードへの影響・未決事項を整理した計画書です。**この文書自体はコード変更を含みません。** 着手時は各Phaseごとに`EnterPlanMode`で詳細設計を行うことを想定しています。

---

## 1. この文書の使い方

引き継ぎ書はシステム間仕様(API・Webhook・データ構造)としては完成度が高く、そのまま踏襲します。一方で、コードベースを実際に読むと**引き継ぎ書が想定していなかった既存資産**がいくつか見つかりました。これらを踏まえずに引き継ぎ書のコード例をそのまま実装すると、既存の仕組みと重複するカラム・二重管理になる箇所があります。本書は以下を目的とします。

1. 「引き継ぎ書の設計 → 新規追加」と「引き継ぎ書の設計 → 既存資産の再利用」を仕分けする
2. 引き継ぎ書のPhase 0〜4を、本コードベースの依存関係に照らして順序を検証する
3. 着手前に人間の判断が必要な論点を明示する

---

## 2. 現状コードベースとの照合で判明したこと(重要)

### 2.1 引き継ぎ書の想定より有利な点

| # | 引き継ぎ書の記述 | 実際のコードベース | 示唆 |
|---|---|---|---|
| 1 | `Product.erp_material_code`を新規追加(§9.1) | `Product.erp_material_id`が**既にFKとして存在**(`crm_mvp/models/pricing.py`)。`ErpMaterial.material_code`に実コードが入っている | **新規カラムを追加せず、既存FK経由(`product.erp_material.material_code`)で`product_code`を組み立てる。** 二重管理を避けられる。ただし`erp_material_id`が未設定の商品をどう扱うかは§5.4の「品目マッピング未整備」ケースと同じ扱いにする |
| 2 | `Contract.erp_sales_order_number`を新規追加(§9.1 IF-25) | `Contract.external_system`/`external_id`が**既に存在**し、コメントに「将来ERPのSalesOrder/BillingDocumentと同期する際のキーになる」と明記(`crm_mvp/models/quoting.py`)。`Account`/`Engagement`と同じ汎用外部キーパターン | **新規カラムを追加せず、`external_system="erp"` / `external_id=document_number`で受ける。** 設計思想として最初から用意されていた拡張点 |
| 3 | 実績3層(契約額/出荷/請求)を`ContractFulfillment`で新設(§9.3) | `Contract.realized_amount`が**既に存在**し、`revenue_report.py`は`realized_amount`があればそちらを`total_amount`より優先する設計が**既に入っている**(コメント: 「同期が始まった時にレポート側の変更を不要にする」ため) | 単純な実績合計だけなら`realized_amount`を更新するだけで売上レポートは無改修で正しい値を返す。**`ContractFulfillment`(出荷/請求/返品の内訳・受注残・消化率)は、`realized_amount`更新に加えて実装する追加機能** — 置き換えではなく上乗せとして計画する |
| 4 | Enum拡張(`QuoteStatus.ISSUABLE`追加等)は`ALTER TYPE ... ADD VALUE`がトランザクション内で実行できないため注意(§9.4) | 本コードベースの`QuoteStatus`/`ContractStatus`/`ComplianceOutcome`等は**すべて`String(N)`カラム+Python `StrEnum`**で実装されており、SQLAlchemyのネイティブ`Enum`型は一切使われていない(全モデルファイルを確認済み) | **PostgreSQLのネイティブEnum型ではないため、値追加にマイグレーション分割は不要。** Pythonの列挙型に値を足すだけで完結する(既存の`ReviewStatus`/`EngagementRelationshipType`追加時と同じ軽さ)。引き継ぎ書§9.4の注意はこのコードベースには当てはまらない |
| 5 | `ScreeningPort`の拡張方針(§3.3-1, §6.3) | 既に`ScreeningPort`(Protocol)・`MockScreeningAdapter`・`AITMScreeningAdapter`が実装済み(`crm_mvp/ports/screening.py`)。`AITM_SCREENING_URL`未設定時は明示的に`RuntimeError`を送出する設計も既に入っている | そのまま拡張できる。`screen()`のシグネチャに`end_user`/`trigger`を足す変更のみで、抽象自体の作り直しは不要 |
| 6 | RLSポリシーを新規テーブルに適用(§9.3) | `weekly_review`/`report_snapshot`で確立済みの定型パターンがある(`ENABLE/FORCE ROW LEVEL SECURITY` + `CREATE POLICY tenant_isolation`をマイグレーション内で実行) | `outbox_message`/`webhook_event`/`contract_fulfillment`/`deemed_export_activity`すべてこのパターンをコピーすればよい。手順は確立済みでリスクは低い |
| 7 | `GatePolicy`の鮮度概念を審査有効期限に流用(§5.6) | `decays_at`/`compute_decays_at`(`crm_mvp/services/decay_policy.py`)が既に`QualificationSlot`で稼働中 | 概念の流用は可能。ただし`decays_at`は`QualificationSlot`専用の実装になっているため、`aitm_valid_until`は**別途`Quote`/`Contract`の独立カラムとして持ち**、表示ロジック(「あとN日」バッジ)だけ`decays_at`表示の既存UIパターンを踏襲するのが現実的 |

### 2.2 引き継ぎ書どおり新規に必要な点(既存資産なし)

- `ComplianceStatus`に`quote_id`/`contract_id`FK、`party_role`(counterparty/end_user)がない → **新規追加が必要**(現状`account_id`のみ)
- `ComplianceOutcome`(`CLEAR`/`HIT`/`NEEDS_REVIEW`/`BLOCKED`/`UNKNOWN`の5値)は引き継ぎ書の`ComplianceStatusValue`(9値: `PENDING`/`FLAGGED`/`PENDING_LICENSE`/`OVERRIDDEN`/`WITHDRAWN`が追加で必要)より少ない → **Python Enumへの値追加のみ**(§2.1-4の通り軽い変更)
- `Contact`に国籍(`nationality`)フィールドがない → みなし輸出判定に新規必要
- `Account`に`aitm_party_id`/`legal_name_en`/`aliases`/`credit_limit`等がない → 新規追加が必要
- `Engagement`に`aitm_rnd_case_id`/`exclude_from_pipeline`がない → 新規追加が必要
- `Outbox`/`Webhook冪等性テーブル`/署名検証ミドルウェアは一切存在しない → **全面新規**(§4.1相当の共通基盤)

### 2.3 既存コードの契約を変更する必要がある点(要注意)

| 対象 | 現状 | 変更内容 | 影響ファイル |
|---|---|---|---|
| `POST /webhooks/sanctions-list-updated`のレスポンス | `ReevaluationOut`(`affected_accounts`/`affected_engagements`)を返す実装が**今回のセッションで作成済み** | 引き継ぎ書§7.2「Webhookのレスポンスに業務データを載せない」方針に合わせ、受理応答のみに変更 | `crm_mvp/api/webhooks.py`、対応する既存テストがあれば要修正 |
| `Stage`列挙 | `LEAD/PROSPECT/QUALIFIED/PROPOSAL/NEGOTIATION/CLOSED_WON/CLOSED_LOST`の7値。`OPEN_STAGES`(`snapshot.py`)・`dashboard.py`・`forecast_risk.py`・`revenue_report.py`の`STAGE_REPORT_LABELS`など**Stageを列挙する箇所すべて**でこの7値を前提にしたロジックがある | `RND_INCUBATION`を追加し、`exclude_from_pipeline=True`のEngagementは上記の集計対象から除外するようフィルタを追加 | `crm_mvp/services/snapshot.py`, `dashboard.py`, `forecast_risk.py`, `revenue_report.py`, 関連テスト多数 |
| `QuoteStatus`列挙 | `DRAFT/SENT/ACCEPTED/REJECTED/EXPIRED`の5値 | `ISSUABLE`を追加し、`DRAFT → ISSUABLE`遷移をゲート判定経由に限定する制御を`quoting.py`に追加 | `crm_mvp/services/quoting.py`, `quotes.html`, `engagement_detail.html`, 関連テスト |

---

## 3. 推奨実装順序

引き継ぎ書のPhase 0〜4の依存関係(§12)は妥当であり、そのまま踏襲することを推奨します。本コードベース固有の知見を踏まえた**補足**のみ以下に示します。

```
Phase 0(共通基盤)
  └─ §2.1-6のRLSパターンをそのまま複製できるため、見積もりの19人日から
     大きく乖離しないはず。ただし C0-4(署名検証)は本CRMに認証機構が
     皆無なため、新規の認証層として慎重に設計・レビューする(§14-9参照)。

Phase 1(2段階審査とゲート制御)
  └─ C1-1(Product.erp_material_codeの編集UI)は §2.1-1 の通り
     「新規カラム」ではなく「既存 erp_material_id の選択UI」に読み替える。
     見積もり・レポート層への影響(§2.3)がこのPhaseに集中するため、
     既存テストスイート(500件超)の回帰確認をタスクに明示的に含める。

Phase 2(取引先とエンドユーザー)
  └─ C2-2(エンドユーザーのデータモデル)は引き継ぎ書§14-5が
     「後付けが困難」と明記する通り、Phase 1 の Quote/Contract 拡張と
     同時に設計すること(別マイグレーションに分割するのは可だが、
     設計は同時に行う)。

Phase 3(ERP連携)
  └─ C3-1(IF-32)は §2.1-2 の通り Contract.external_system/external_id を
     再利用するため、引き継ぎ書のタスクよりやや軽くなる見込み。
     C3-6(実績3層)は §2.1-3 の通り、realized_amount 単体更新であれば
     売上レポートは既に対応済み — ContractFulfillment 導入前でも
     「実績を反映する」という業務価値の一部は先出しできる
     (Phase 3 内で優先度を分けることを推奨)。

Phase 4(ライセンス・R&D・みなし輸出)
  └─ C4-3(RND_INCUBATIONステージ)は §2.3 の影響範囲(Stageを列挙する
     既存箇所すべて)を洗い出すチェックリストをタスク着手時に必ず作る。
```

---

## 4. Phase別タスク一覧(コードベース注記付き)

引き継ぎ書§12のタスクID(C0-1〜C4-11)をそのまま使用します。ここでは**本コードベースに関する追加注記のみ**記載します(工数目安・依存関係は引き継ぎ書を参照)。

### Phase 0 — 共通基盤

**進捗(2026-08-15): C0-2/C0-3/C0-7 実装済み・全テストgreen(582件)。C0-1は
Phase 1へ意図的に先送り(コンシューマ不在のため)。C0-4はスコープを絞り
`verify_webhook`/`record_webhook_event`を単体実装のみ済み、既存2エンドポイント
への接続はPhase 1へ送った(理由はPhase 0実装計画のContext参照)。**

| ID | 追加注記 |
|---|---|
| C0-1 | 追加カラムは`Account`/`Contact`/`Engagement`/`Quote`/`Contract`。§2.2の一覧を新規分の正とする(`erp_material_code`/`erp_sales_order_number`は追加しない、§2.1参照)。**[Phase 1へ先送り]** |
| C0-2 | `outbox_message`/`webhook_event`のRLSは`weekly_review`マイグレーション(`alembic/versions/82df2c8b7c29_*.py`)をテンプレートにする。**[完了]** `crm_mvp/models/integration.py`+`alembic/versions/0943c90da570_*.py` |
| C0-3 | `crm_mvp/services/integration_client.py`として新設。既存`ports/screening.py`の`httpx`利用パターンを流用可能。**[完了]** `SignedClient`(integration_client.py)+`classify_http_response`/Outbox本体(services/outbox.py)+`scripts/process_outbox.py` |
| C0-4 | 既存`crm_mvp/api/webhooks.py`の`get_tenant_id`(ヘッダのみで真正性検証なし)を置き換える。**画面側の認証(Cookieベース擬似セッション)とは完全に独立させる**こと — 混同すると`crm_tenant_id`Cookieの弱さがAPI側にも波及する。**[部分完了]** `crm_mvp/api/webhook_security.py`(`verify_webhook`/`record_webhook_event`)は単体実装・テスト済み。既存2エンドポイントへの接続はPhase 1(IF-10/IF-12の`revision`実装と合わせて)に送った。あわせて`sanctions-list-updated`のレスポンス契約修正(業務データ除去→ActionItem化)を先行実施済み |
| C0-7 | 新規画面。既存の`/ui/reports/history`(スナップショット履歴)と似た「一覧+詳細+手動操作」構成が流用できる。**[完了]** `/ui/integration-status`(`crm_mvp/api/web/integration_status.py`) |

### Phase 1 — 2段階審査とゲート制御

**進捗(2026-08-15): Phase 1a(往復導線)+ Phase 1b(ハード遮断)実装済み・
全テストgreen(614件、うち新規32件)。文書出力(C1-10)のみ意図的に未着手 —
詳細は下表参照。**

| ID | 追加注記 |
|---|---|
| C1-1 | 「新規カラム編集UI」ではなく「`Product`編集画面(`/ui/products`)に`erp_material_id`選択欄を追加」と読み替える。未マッピング一覧は`Product.erp_material_id IS NULL AND is_active`で抽出可能。**[未着手]** 既存の`/ui/products`編集フォームは`erp_material_id`選択欄自体は無いが、品目未マッピングは`submit_provisional_review`/`submit_formal_review`がActionItem起票で可視化する形で当面代替(Phase 1a完了分) |
| C1-2〜C1-6相当 | AI_TMへの仮審査・正式審査の起票。**[完了]** `crm_mvp/services/review_case.py`(`build_review_key_hash`/`submit_provisional_review`/`submit_formal_review`/`dispatch_aitm_review_submit`)。Outbox経由(Phase 0基盤)で非同期送信、`ReviewCase`モデル新設(`crm_mvp/models/review_case.py`)。`Quote`/`Contract`に`destination_country`/`end_user_account_id`/`end_use`列を追加(旧C0-1相当、ここで初めて実消費者ができた) |
| C1-7相当 | IF-10受信ハンドラ。**[完了]** `/webhooks/aitm/review-result`(`crm_mvp/api/webhooks.py`)。Phase 0で単体実装のみだった`verify_webhook`/`record_webhook_event`をここで初めて実ルートに接続。`revision`によるstale判定も実装済み |
| C1-8 | `QuoteStatus.ISSUABLE`追加は§2.3参照。**[完了・ただし設計を変更]** 新しい中間ステータスは追加せず、`crm_mvp/services/review_case.py`の`check_review_clearance()`を`update_quote_status_ui`/`update_contract_status_ui`(`crm_mvp/api/web/engagements.py`)に挟み、既存の`SENT`/`SIGNED`/`ACTIVE`遷移そのものをゲートする形にした。理由: `QuoteStatus.ISSUABLE`を追加すると`crm_mvp/api/web/quotes.py`の3つのハードコードされた並行マップ(`QUOTE_STATUS_LABELS`/`ORDER`/`BADGE`)や一覧テンプレートまで広範囲に手を入れる必要があり、同じ業務要件がもっと小さい変更で実現できたため |
| C1-9 | `GateKind.EXPORT`/`GateKind.COMMERCE`は、既存`ComplianceCheckType`から導出できないか検討する(§6の未決事項1)。**[COMMERCE側は先送り、EXPORT側は完了]** ERP側(与信・反社)連携がまだ存在しないため、ブロックできるのはAI_TM(輸出)側の`ReviewCase`判定のみ。`GatePolicy`の汎用条件式化は過剰設計と判断し見送った — COMMERCE側はPhase 2でERP連携が入った時点で再検討 |
| C1-10 | ドキュメント出力の既存実装箇所は未調査(現状PDF出力機能自体が無い可能性が高い)。**着手前に見積書/契約書の出力機能の現状有無を確認すること**(§6の未決事項2)。**[確認済み・未着手]** grepで確認した結果、PDF/文書出力機能は本コードベースに一切存在しない。それ自体が別途スコープすべき前提タスク |
| (副産物) | `crm_mvp/services/artifact_gate.py`(ARTIFACT種別ゲート評価)を新設。`seed_policies.py`に元々投入済みだった`artifact.quote`/`artifact.contract`ポリシーが、評価関数の不在により完全に無効だったのを有効化した(Phase 1aのスコープには無かったが、`gate_engine.evaluate_gate()`が種別非依存で再利用可能だったため小さく含めた) |

**既知の残課題(Phase 1bで意図的に先送り)**: (1) 有効期限切れ審査の能動的な
`SENT→DRAFT`自動差し戻し(現状は送付操作時のその場チェックのみ)、(2) 未クリア
審査の手動再起票UI(現状の回避策: 品目マッピング修正後に見積を作り直す)。

### Phase 2 — 取引先とエンドユーザー

**進捗(2026-08-15): 全項目着手・実質完了。631件のテストは全てgreen(新規機能
分の追加テストは今回未作成 — AI_TM側の開発が進んでから改めてテストを
充実させる方針のためスキップ、既存回帰のみ確認)。**

| ID | 追加注記 |
|---|---|
| C2-1 | `build_party_ref()`は`Account`の`external_system`/`external_id`(既存)と`aitm_party_id`(新規)を組み合わせるだけで、既存パターンの延長。**[完了]** `crm_mvp/services/party_compliance.py`に追加。`review_case.py`の審査起票ペイロードに`counterparty_ref`/`end_user_ref`として組み込み済み |
| C2-2相当 | エンドユーザーのデータモデルと選択UI。**[完了]** Phase 1aで追加済みだったが一度も書き込まれていなかった`Quote`/`Contract`の`destination_country`/`end_user_account_id`/`end_use`列を、`create_quote_from_engagement`/`create_contract`(`crm_mvp/services/quoting.py`)のキーワード引数として実際に埋める導線と、`engagement_detail.html`の見積・契約作成フォームに選択欄を追加した。契約は見積の設定を自動継承する |
| C2-3相当 | `crm_mvp/services/compliance_screening.py`を新設し、`accounts.py`の手動トリガーと自動フックの両方が使う共通ロジック(`run_compliance_check`/`ensure_account_screened`)に整理した |
| C2-4/C2-5 | Account作成時の自動スクリーニングフック・見積前の鮮度チェック。**[完了]** `engagement_new_submit`(新規商談画面)・`convert_lead`(Lead変換)・`create_child_engagement`(継続/Upsell/Cross-sell)の3経路すべてに`ensure_account_screened`を配線。`create_grouping_account`(法人グループの箱)は意図的に対象外とした理由をdocstringに明記。鮮度判定は`ComplianceStatus.is_fresh`を再利用し、freshなら再スクリーニングしない |
| C2-8相当(worse-case-wins) | 商談・見積・契約の作成ブロック(§8.1/§8.3)。**[完了]** `check_party_clearance()`を見積・契約作成に加え、Engagement作成の3経路すべてにも配線した。今回は`HIT`のみ判定(`NEEDS_REVIEW`/`BLOCKED`/`UNKNOWN`は将来課題) |
| C2-6/C2-7 | IF-11(`screening.alert`/`party.linked`)受信。**[完了]** 新規`/webhooks/aitm/party-event`(`crm_mvp/api/webhooks.py`)。`party.linked`は`Account.external_system`/`external_id`/`aitm_party_id`を更新、`screening.alert`は`ComplianceStatus`更新+進行中商談へのActionItem起票(`/webhooks/sanctions-list-updated`と同じ設計原則)。IF-12(既存`receive_compliance_judgment`)は変更なし(既に汎用的) |
| C2-9 | 取引先詳細UI。**[完了]** `account_detail.html`にコンプライアンスカードを追加(ERP登録バッジ・`aitm_party_id`・チェック種別ごとの状態表/期限切れ表示・手動再スクリーニングボタン) |
| C2-10 | IF-02バッチスクリーニング。**[完了]** `scripts/batch_screening.py`(`process_outbox.py`と同じCLI形状) |

### Phase 3 — ERP連携

**進捗(2026-08-15): コア部分(C3-1/C3-2/C3-3/C3-4/C3-5/C3-6/C3-7/C3-8)実装済み。
全テストgreen(631件、新規テストは今回未作成 — Phase 2remainder以降と同じ
方針)。ダミー実装のERP側と同じ「同期レスポンス」契約を前提に組んでいる
ため、実ERPのAPI契約が固まった時点で`crm_mvp/services/commerce_check.py`/
`erp_transcription.py`のペイロード形状を再確認すること。**

| ID | 追加注記 |
|---|---|
| C3-1/C3-2 | §2.1-2の通り、レスポンスの`counterparty_attributes`を`Account`に反映する処理を中心に実装。**[完了]** `crm_mvp/services/commerce_check.py`。IF-32は同期レスポンス型のため、Outbox dispatcherがその場で`ComplianceStatus`(CREDIT/ANTI_SOCIAL)と`Account.credit_limit`等を更新する — 別途受信Webhookは不要と判断した。`ng`は新設の`check_commerce_clearance()`(`party_compliance.py`)で見積送付・契約締結をハード遮断する(既存の`check_review_clearance`と並列で`update_quote_status_ui`/`update_contract_status_ui`に配線) |
| C3-3 | IF-25送信時、`Contract.external_system="erp"`が既に設定されている(=ERP登録済み取引先からの契約)場合と、未設定(=CRM発生の新規取引先)の場合で分岐が必要。**[完了]** `crm_mvp/services/erp_transcription.py`。`aitm_transaction_id`(§6.9最重要)は正式審査`ReviewCase.provider_request_id`から取得して必ず引き渡す。契約`SIGNED`遷移時に自動起票、レスポンスの`document_number`を`Contract.external_id`に保存(以後IF-29/30/31が逆引きするキーになる) |
| C3-4/C3-5 | IF-26/IF-27(品目・取引先マスタ受信)。**[完了]** 新規`crm_mvp/api/erp_webhooks.py`(`/webhooks/erp/material-updated`・`/business-partner-updated`)。CSV取込スクリプトが使っている`upsert_erp_material`/`upsert_erp_business_partner`をそのまま1件ずつ呼ぶ薄いラッパーとして実装 — 取込ロジックの二重実装を避けた |
| C3-6/C3-7 | 優先度分割を推奨: (a)`realized_amount`更新のみ(既存レポート即対応) → (b)`ContractFulfillment`による内訳・受注残の追加、の2段階に分けて価値を早期に出す。**[両方完了]** 新規`ContractFulfillment`モデル(`crm_mvp/models/fulfillment.py`)+`crm_mvp/services/fulfillment.py`。`/webhooks/erp/delivery-posted`・`billing-posted`・`return-posted`(`erp_webhooks.py`)が受信し、`recalculate_actuals()`が`Contract.realized_amount`(既存フィールド、`revenue_report.py`が優先使用)を都度再計算する。冪等性は`(tenant_id, kind, erp_document_number)`のユニーク制約で担保 |
| C3-8 | 契約詳細の実績3層UI・時系列分析。**[最小限完了]** `engagement_detail.html`の契約カードに受注残・未請求残・消化率の1行サマリーを追加。時系列分析(グラフ等)は未着手 |
| C3-9 | IF-28与信枠照会(見積時のリアルタイム参照)。**[未着手]** C3-1のレスポンス反映で`Account.credit_limit`/`credit_available`は既に見積作成の都度更新されるため、別途の同期照会エンドポイントは優先度を下げた |

### Phase 4 — ライセンス・R&D・みなし輸出

**進捗(2026-08-15): コア部分(C4-1/2/3/4/5/6/7/8/11)実装済み。全テストgreen
(631件、新規テストは今回未作成・Phase 2以降と同じ方針)。C4-9/C4-10
(規制情報照会・事前警告)のみ意図的に未着手。**

| ID | 追加注記 |
|---|---|
| C4-1/C4-2 | IF-06残枠照会・IF-07仮引当/解放。**[完了]** 新規`LicenseAllocation`モデル+`crm_mvp/services/license.py`。IF-32(商流ゲート)と同じ「同期レスポンス型」を仮定してOutbox dispatcherがその場で結果を反映する設計にした。見積作成時に照会、契約締結(SIGNED)時に仮引当、契約TERMINATED時に解放(§6.6 IF-08連動)を自動起票する |
| C4-3 | `RND_INCUBATION`追加前に、§2.3の影響一覧(`OPEN_STAGES`、ダッシュボード、フォーキャストリスク、売上レポートのステージラベル)を実装者が実際にgrepして洗い出すことをタスクの最初のステップとして明示する。**[完了]** `Stage.RND_INCUBATION`を追加、`stage_transitions.py`の`STAGE_ORDER`には意図的に含めない(通常のゲート付き遷移の対象外)。`Engagement.exclude_from_pipeline`でダッシュボードのパイプライン集計から除外。`crm_mvp/services/rnd_opportunity.py`(`process_rnd_opportunity`/`promote_rnd_engagement`)+新規`/webhooks/rnd-opportunity`(IF-14受信) |
| C4-4 | R&D起点商談の一覧・昇格操作UI。**[完了]** 新規`/ui/rnd-opportunities`(`crm_mvp/api/web/rnd_opportunities.py`)。一覧+「商談化する」ボタンのみの最小画面 |
| C4-5 | IF-09みなし輸出イベントの送信。**[完了]** `crm_mvp/services/deemed_export.py`。`IngestionSource`(活動ログ)に技術情報授受フラグが立った時点でOutbox経由送信 |
| C4-6 | `Contact.nationality`は新規追加。既存の`IngestionSource`/`Touch`テーブルへの「技術情報授受」フラグ追加は`DeemedExportActivity`の新設で代替可能か検討(§6の未決事項3)。**[完了]** `IngestionSource`に`involves_technical_disclosure`/`deemed_export_event_type`を追加(新規テーブルは作らず既存テーブルに列追加する方を採用)。`Contact.nationality`も追加。情報投入フォーム(`/ui/sources/new`)にチェックボックス+活動種別選択+注意喚起文を追加。参加者国籍(`participants`JSONBからContactへの自動突合)は未実装 — スコープ外として明記 |
| C4-7 | IF-13みなし輸出リスクの受信・表示。**[完了]** 新規`/webhooks/deemed-export-risk`。新規Account列は追加せず、既存`ComplianceStatus`(`check_type=export_control`)を「みなし輸出注意」フラグの置き場として再利用した |
| C4-8 | IF-16継続監視アラートの受信・更新商談ブロック。**[完了]** 新規`/webhooks/contract-monitoring`が`Contract.monitoring_alert`を立て、`create_child_engagement`(renewal時)がこれを見てブロックする |
| C4-9/C4-10 | IF-04/IF-05品目・判定履歴の照会とキャッシュ、見積品目追加時の規制事前警告。**[未着手]** ゲート判定・作成ブロックのようなコア業務要件に直結しないため優先度を下げた。実AI_TM連携時に改めてスコープする |
| C4-11 | ダッシュボードの各カード。**[完了]** 審査待ち件数・連携要対応(Outbox failed/dlq)件数・R&D起点商談件数・継続監視アラート契約件数の4枚をダッシュボード上部に追加 |

---

## 全体進捗サマリー(2026-08-15時点)

Phase 0〜4のコア部分をすべて実装済み。累計コミット: `b602d81`(Phase 0)〜
本コミット(Phase 4)。全631件の既存回帰テストはgreenを維持しているが、
**Phase 2以降(Phase 2remainder・Phase 3・Phase 4)は新規の自動テストを
意図的に作成していない** — AI_TM/ERP側の開発が具体化し実APIの契約が
固まった時点で、実際のレスポンス形状に合わせてテストを整備する方針
(ユーザー指示による)。各サービスモジュールの新規パスは手動の
functional smoke testで動作確認済み。

主な未着手・既知の簡略化:
- ドキュメント出力制御(C1-10): 本コードベースにPDF/文書出力機能自体が存在しない
- IF-04/IF-05規制情報照会・キャッシュ(C4-9/C4-10)
- 参加者国籍の自動突合(`participants`→`Contact.nationality`)
- 真のFX換算(通貨ごとのバケット化で代替)
- COMMERCE側の対象check_typeは`CREDIT`/`ANTI_SOCIAL`固定(§8.1の全状態機械ではなく`CLEAR`/`NEEDS_REVIEW`/`BLOCKED`の3値に単純化)

---

## 5. 未決事項(着手前に人間の判断が必要)

1. **`ComplianceStatus.gate_kind`は新規カラムか、`check_type`からの導出か。** 既存`ComplianceCheckType`(`ANTI_SOCIAL`/`CREDIT`/`SANCTIONS`/`EXPORT_CONTROL`)は既に「商流系」「輸出系」に自然に二分できる。新規カラムを持たせると二重の真実になりうるが、クエリの単純さでは新規カラムに利点がある。**方針決定が必要。**
2. **見積書・契約書のPDF出力機能は現状CRMに存在するか。** 引き継ぎ書§8.2「ドキュメント出力制御」は既存機能への制御追加を前提にしているが、現行コードベースには出力機能そのものが見当たらない可能性がある。**存在しない場合、Phase 1の工数見積り(C1-10: 5人日)は「出力制御の追加」ではなく「出力機能自体の新規実装+制御」になり、大幅に膨らむ。**
3. **みなし輸出の「技術情報授受フラグ」は既存`IngestionSource`/`Touch`への属性追加か、`DeemedExportActivity`単独か。** 引き継ぎ書は両方を書いているが役割が重複して見える。一本化を検討。
4. **`Product.erp_material_id`が未設定の商品(=ERP品目と未マッピング)の現存数。** §5.4(品目マッピングの棚卸し)は業務準備タスクだが、現状何件の`Product`が未マッピングかをPhase 1着手前に集計しておくと、C1-1の実際の作業量が見積もれる。
5. **`AITM_INTEGRATION_MODE`/`ERP_INTEGRATION_MODE`/`COMMERCE_CHECK_MODE`の環境変数運用は、既存の`AITM_SCREENING_URL`未設定時`RuntimeError`方式と揃えるか、`mock`/`live`切替方式に変えるか。** 既存実装は「未設定なら例外」、引き継ぎ書は「モード変数で明示切替」。両立は可能だが方針を決めておく。

---

## 6. ビジネス側の前提条件(実装外)

引き継ぎ書§5.4・§14-4が明記する通り、以下は**エンジニアリングタスクではなく業務準備タスク**です。開発着手と並行して進めないと、Phase 1完成後も審査が起票できません。

- 審査対象になりうる`Product`について、ERP品目コード(`ErpMaterial`)との対応関係の棚卸し・欠損分の登録
- `AITM_BEARER`/`AITM_*_SIGNING_SECRET`等のシークレット発行手続き(AI_TM側・ERP側それぞれ)
- 与信・反社チェック(IF-32)がERP側で正式実装されるまでの運用(現状ダミー`OK`固定)の業務影響範囲の合意

---

## 7. 着手判断のためのチェックリスト

Phase 0着手前に、以下が揃っていることを確認してください。

- [ ] §5の未決事項1〜5について方針が決まっている
- [ ] AI_TM側・ERP側それぞれの署名シークレットの発行・受け渡し方法が決まっている(開発環境用のダミー値を含む)
- [ ] `ERP_BASE_URL`/`AITM_*_URL`の開発環境向けエンドポイントが確保されている(モック含む)
- [ ] 既存テストスイート(2026-08-14時点で500件超)がgreenであることを起点として記録している(回帰確認の基準点)
- [ ] Phase 0〜4のどこまでを最初のマイルストーンとするか(全Phase一括ではなく、区切りの合意)

---

## 8. 参照文書

- `CRM_連携引き継ぎ書.md` — 本計画のソースとなる仕様書(API・データ構造の一次情報はこちらが正)
- `CRM_USER_GUIDE.md` — 本CRMの現状ユーザーガイド(§6が本連携と関係する既存インターフェースを整理済み)
- `CRM_INTEGRATION_HANDOVER.md` / `erp_crm_spec.md` — AI_TM/ERP側から見た仕様書(旧版。引き継ぎ書がこれらを統合・更新した位置づけ)
