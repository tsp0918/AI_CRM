# Compliance-aware Agentic CRM — ユーザーガイド

文書バージョン: 1.0
作成日: 2026-08-14
ステータス: 現状(as-built)の実装に基づく記述

---

## 0. この文書について

### 目的

本書は、このCRM(以下「本CRM」)を**実際に運用する立場**と、**他システム(AI Trade Management / ERP / 将来的な別CRM等)と組み合わせて業務設計する立場**の両方に向けたガイドです。

- 現場の営業・マネージャー・マーケ担当が「どの画面で何をするか」を理解する
- システム連携の設計者が「本CRMが何を持ち、何を持たないか」「どこにAPIやWebhookの受け口があるか」を理解する
- ERP・AI Trade Management(以下「AI_TM」)を含めた3システム構成の中で、**どのシステムが何のSystem of Recordか**を混乱なく判断する

### 位置づけ — 既存の引き継ぎ書との関係

本リポジトリには既に以下の文書があります。本書はこれらを置き換えるものではなく、**本CRM側の視点から見た「読み手が逆」の文書**として機能します。

| 文書 | 視点 | 主な内容 |
|---|---|---|
| `HANDOVER.md` | 本CRMの設計者 → 本CRM開発者 | 不変条件・思想・実装ロードマップ(やや古い箇所あり、§0参照) |
| `CRM_INTEGRATION_HANDOVER.md` | AI_TM開発チーム → 本CRM開発チーム | AI_TMのAPI仕様・Webhook設計 |
| `erp_crm_spec.md` | ERP開発チーム → 本CRM開発チーム | ERPのデータモデル・API仕様 |
| **本書 `CRM_USER_GUIDE.md`** | **本CRM → 利用者・連携先すべて** | **本CRMの使い方・データモデル・公開インターフェース** |

`HANDOVER.md`は当初「ERPやAI_TMに寄り添うコンパニオン」として本CRMを設計する意図で書かれていますが、その後の開発でLead獲得〜商談〜見積・契約・契約更新・週次レビュー・経営レポートまで、当初`HANDOVER.md`§6「スコープ外(作らない判断)」としていた範囲も含めて実装が進みました。本書は**2026-08-14時点の実装の実態**を記述します。設計思想の由来を知りたい場合は`HANDOVER.md`を、外部システムのAPI仕様の一次情報が必要な場合は`CRM_INTEGRATION_HANDOVER.md`/`erp_crm_spec.md`を参照してください。

---

## 1. プロダクト概要

### 1.1 何をするCRMか

**「Lead から Contract まで、一つのオブジェクトグラフで貫くCRM」**。ステージだけを記録する箱ではなく、以下を一気通貫で扱います。

- リード獲得〜案件化(Lead → Engagement)
- 商談の進行管理(ステージ・ゲート判定・証拠の強度)
- バイヤー相関図(誰が決裁者か、誰が到達済みか)
- 見積・契約(CPQライク、商品マスタは本CRM内で完結)
- 契約更新・Upsell/Cross-sell管理
- 週次レビュー・アクションタスク(1on1運用)
- キャンペーン・シーケンス(マーケティング自動化)
- 経営レポート・時系列スナップショット
- コンプライアンスステータス(反社・与信・輸出管理判定の**受け皿**。判定ロジック自体は外部委譲)

### 1.2 対象ユーザー(ICP)

日本〜APACの中堅〜エンタープライズ製造業を想定して構築されていますが、実証検証(後述)により**業種非依存で機能する**ことを確認済みです。訴求軸は「証跡ベースの確度スコア」と「与信・反社・輸出管理を含む取引管理」です。

### 1.3 重要な制約(運用設計上、必ず把握すること)

| 制約 | 内容 |
|---|---|
| **認証機構なし** | ログイン・パスワード・トークン認証は実装されていません。`crm_tenant_id`/`crm_actor_id`をCookieに保存するだけの擬似セッションです。**社内ネットワーク限定運用、または前段にリバースプロキシでの認証を必ず追加してください。** |
| **RBAC/権限管理なし** | 「マネージャーだけがこの操作をできる」という権限制御は実装されていません。役職(`AuthorityLevel`)はUser情報として保持しますが、実際の書き込み制御には使われていません。 |
| **テナント分離はRow-Level Security** | `X-Tenant-Id`ヘッダ(JSON API)またはCookie(画面)でテナントを識別し、PostgreSQLのRLSポリシーで強制分離しています。誰でも任意のテナントIDを名乗れる点は認証なしの制約と表裏一体です。 |
| **通貨換算は行わない** | 複数通貨が混在する場合、集計は通貨ごとに分けて表示されます(2026-08-14修正)。為替レートによる合算はしません。 |
| **外部連携は受け口のみ実装、送信側は未実装の箇所あり** | 詳細は §6 を参照。 |

---

## 2. 全体構成 — 画面・機能マップ

サイドナビの並びに沿って整理します(`crm_mvp/api/templates/_base.html`)。

```
ダッシュボード                 … 案件一覧・確度スコア・週次レビュー未対応バッジ・更新未着手警告
⚡ クイック入力(サイドバー常設) … 担当者を選んでその場で活動ログを残す
+ 新規案件
提案承認                       … AI抽出結果(ExtractionProposal)の承認待ち一覧

[案件・取引先]
リード                         … 案件化前の見込み客(ステータス/優先度パイプライン表示)
取引先                         … Account一覧(法人グループのロールアップ対応)
見積・契約                     … 全案件横断の見積・契約一覧、担当別/商品別/取引先別ドリルダウン
契約更新                       … 更新未着手のACTIVE契約を期限バケット別に表示

[マーケティング]
キャンペーン                   … リード獲得施策とROI(リード数・転換率)
シーケンス                     … アウトバウンド自動化(分岐付きステップ・ドラフト生成)

[レポート]
売上レポート                   … 受注実績を商品グループ/取引先/セールスグループ/関係性で集計
レポートビルダー               … 行×列を自由に選べるクロス集計(受注のみ/全ステージ切替可)
リスクレーダー                 … 予測未達リスクのある案件を自動ランキング
スナップショット履歴           … 任意時点の経営指標を保存・時系列比較(2026-08-14追加)

[マスタ管理]
ERP取引先 / ERP品目            … ERPからのCSV取込結果の参照専用ビュー
商品                           … 本CRM独自の商品マスタ(Product) — §6.5参照
セールスグループ               … 組織階層(担当者集計用の軽量タグ)
ユーザー                       … 担当者ロースター(認証アカウントではない)
```

商談個別の画面(`/ui/engagements/{id}`)は最もリッチな画面で、詳細は §4.2 で説明します。

---

## 3. コアオブジェクトモデル

### 3.1 関係図(概略)

```
Account(取引先) ─┬─< Contact(人物)
                  ├─< Engagement(商談) ─┬─< EngagementLineItem(商品構成)
                  │                      ├─< Quote(見積) ─< QuoteLineItem
                  │                      ├─< Contract(契約) ─< ContractLineItem
                  │                      ├─< QualificationSlot(証拠: 主張+根拠+強度)
                  │                      ├─< StageTransition(ステージ遷移履歴)
                  │                      ├─< ActionItem(ToDo)
                  │                      ├─< WeeklyReview(週次レビュー、週1行)
                  │                      ├─< PipelineSnapshot(日次スナップショット)
                  │                      └─ parent_engagement_id(自己参照: Renewal/Upsell/Cross-sell)
                  ├─< GraphNode(相関図ノード: 人物 or プレースホルダ)
                  │     └─< GraphEdge(承認関係) / EngagementRole(商談内での役割)
                  └─< ComplianceStatus(反社・与信・輸出管理判定の受け皿)

Lead(案件化前) ──(convert)──> Engagement + Account/Contact
    └─< Touch(接点履歴)   Campaign / Sequence ──enroll──> SequenceEnrollment ─< SequenceDraft

Product(商品マスタ、ProductGroup配下) ── 独自マスタ。ERP品目とは別物(§6.5)
SalesGroup(組織階層) / User(担当者ロースター、認証なし)
ReportSnapshot(経営指標の時系列スナップショット、payloadはJSONBで固定化)
```

### 3.2 各オブジェクトの役割

| オブジェクト | 役割 | 備考 |
|---|---|---|
| `Account` / `Contact` | 取引先・人物 | ERPのBusinessPartnerとは別テーブル。`external_system`/`external_id`で疎結合に紐付け可能(§6.5) |
| `Lead` | 案件化前の見込み客 | `convert_lead()`で`Engagement`化。変換時のスコア・接点サマリーは`conversion_snapshot`に固定保存 |
| `Engagement` | 商談 | `parent_engagement_id`/`relationship_type`(renewal/upsell/cross_sell)で契約更新・拡張を親子関係として表現 |
| `GraphNode`/`GraphEdge`/`EngagementRole` | バイヤー相関図 | 決裁者への到達可否・単一窓口リスクの判定に使う。実名が無くても`placeholder_label`だけで登録可 |
| `QualificationSlot` | 商談の評価軸(BANT/MEDDIC相当) | 「主張(value)＋根拠(evidence)＋証拠強度(confidence)」の3点セットで持つ。テキスト欄に丸めない |
| `GatePolicy`/`GateEvaluation`/`Waiver` | ステージ移行のゲート判定 | 業種テンプレート単位で条件を定義。ブロックされた場合は`Waiver`(例外承認)で通過可能 |
| `Product`/`EngagementLineItem`/`Quote`/`Contract` | CPQライクな見積・契約 | 商品マスタは本CRM内で完結(ERP品目とは別、§6.5参照) |
| `ActionItem` | ToDo | ゲート判定由来(`field_path`が criterion名)と、1on1で決めた自由入力(`field_path="manual"`)の2系統 |
| `WeeklyReview` | 週次レビュー | 商談×週の月曜日で1行。担当者コメント/マネージャーコメント/ステータス(順調・要注意・エスカレーション) |
| `PipelineSnapshot` | 日次のステージ・金額・確度スコアの記録 | 手動またはcron(`scripts/daily_snapshot.py`)で積む。週次レビューの前週比較・パイプライン復元の基礎データ |
| `ReportSnapshot` | 経営指標の時系列スナップショット | 売上・キャンペーン効果・シーケンス効果・パイプラインを1時点=1行としてJSONB保存 |
| `Campaign`/`Sequence`/`SequenceEnrollment`/`SequenceDraft` | マーケティング施策 | シーケンスは分岐付き(反応の有無で次ステップを変える)。実際の配信は行わずドラフト生成まで |
| `ComplianceStatus` | 反社・与信・輸出管理の判定結果 | **本CRムは判定ロジックを持たない**。外部(AI_TM)からの結果を受け取って保持するだけ(§6.3) |
| `SalesGroup` | 組織階層 | 担当者集計用の軽量タグ。権限とは無関係 |
| `User` | 担当者ロースター | ログインアカウントではない。`owner_user_id`として案件に紐付ける表示用の存在 |

### 3.3 人とAIの入力分担(本CRM内部)

`README.md`で定義された3段階モデルがそのまま現行実装の基盤です。

| 段 | 対象 | 確認 | 実装 |
|---|---|---|---|
| AIが直接書く | 参加者の同定、接触履歴、日時、言及トピック | なし | `IngestionSource` → 抽出パイプライン |
| AIが提案し人が承認 | クオリフィケーション値、稟議ルート、態度、クローズ日候補 | 1タップ | `ExtractionProposal` → `/ui/proposals`または`POST /proposals/{id}/accept` |
| 人しか書けない | `VERIFIED`への昇格、ステージ変更、金額確約、例外承認 | — | `extraction_pipeline.NEVER_AI_FIELDS`で機械的に強制 |

入力口は`IngestionSource`(トランスクリプト・録画URI・メール・自由記述メモ)の1つだけに統一されています。「どのフィールドに入れればいいか」を担当者が悩む必要はありません。

---

## 4. 主要業務フロー

### 4.1 リード獲得 → 案件化

1. `POST /ui/leads/new`(手動登録)またはキャンペーン/シーケンス経由でLeadを作成
2. `Touch`(接点)を記録するたびに`WORKING`へ自動昇格、スコア(`company_score`/`person_score`)を計算
3. `MQL`→`SQL`→`convert`で`Engagement`化。変換時のスコア・接点サマリーは`Lead.conversion_snapshot`に固定
4. `/ui/leads`はステータス別・優先度(象限: hot/watch/nurture/low)別のパイプライン表示に対応

### 4.2 商談の進行管理(`/ui/engagements/{id}`)

2026-08-14に大幅改修された画面です。**固定エリア**(常時表示)と**タブ**(担当者向け/マネージャー向け)に分かれます。

**固定エリア:**
- クロージング確度スコア(証拠充実度・稟議到達度・鮮度の3軸)
- 決裁者到達サマリー(登場人物N名/決裁者: 到達済み・経路あり・未到達)
- 週次レビュー(先週=読み取り専用・今週=編集可能の2カード)
- アクションタスク(ToDo表、期限超過は赤バッジ)

**タブ「商談を進める」(担当者向け, 既定):** 商品構成・見積・契約・ステージ/ゲート・クオリフィケーション・提案承認・取込情報・派生商談

**タブ「状況を把握する」(マネージャー向け):** リード発生経緯・最近の活動・バイヤー相関図(フル)

ステージ移行は`GatePolicy`の条件(証拠スロットの強度・グラフ到達性・コンプライアンス鮮度)を満たさない限りブロックされます(`GateStrength.BLOCK`)。例外的に進める場合は`Waiver`を発行します。

### 4.3 見積・契約

商品マスタ(`Product`)から`EngagementLineItem`を積み上げ、`Quote`(見積)→`Contract`(契約)へ変換します。金額・商品名は発行時点でスナップショット(`*_snapshot`列)として凍結されるため、後から商品マスタを変更しても過去の見積・契約表示は壊れません。

### 4.4 契約更新・Upsell/Cross-sell

`/ui/renewals`は「契約終了日が迫っている(または超過している)ACTIVE契約のうち、まだ更新商談が起票されていないもの」を自動抽出します。更新商談は`create_child_engagement(relationship_type=RENEWAL)`で作成し、**親商談の証拠(QualificationSlot)を自動的に引き継ぎます**(2026-08-14追加、鮮度は再計算)。Upsell/Cross-sellは別商材の検討として引き継がず独立して再確認します。

ダッシュボード上部には「更新未着手の契約がN件(うちM件は期限超過)」という警告カードが表示されます。

### 4.5 週次レビュー・1on1運用

商談詳細画面の固定エリアで、週×商談単位のレビューを記録します。マネージャーステータス(順調/要注意/エスカレーション)は既存の色語彙(緑/amber/赤)を流用しています。ダッシュボードには「今週レビュー済みか」のバッジも表示されます。

### 4.6 キャンペーン・シーケンス

`Campaign`はROI集計の単位、`Sequence`は分岐付きのアウトバウンド自動化(メール/架電タスク/LinkedIn)です。実際の配信は行わず、`SequenceDraft`(下書き)の生成までを担当します。反応の有無(`reaction_channels`)で次のステップを分岐できます。

### 4.7 経営レポート・スナップショット履歴

`/ui/reports/revenue`・`/ui/reports/builder`・`/ui/forecast-risk`は常に「今」の状態を見る画面です。一方`/ui/reports/history`は任意時点の状態を保存し、時系列で一覧・詳細・2時点比較ができます。集計関数(売上・キャンペーン・シーケンス)は`as_of`パラメータで過去日付時点を実履歴(StageTransition/Lead作成日時など)から正確に再構成します。パイプラインのステージ内訳だけは、`PipelineSnapshot`が実際にその日付分存在する場合のみ復元可能です。

---

## 5. 業務ロール別にできること(組織設計の指針)

`SalesGroup`(組織階層)と`User.owner_user_id`(案件の担当者)を軸に、以下のような分担を想定して設計されています(権限で強制はされません)。

| ロール | 主な操作 | 関連する画面 |
|---|---|---|
| 営業担当(AE) | 商談進行・見積作成・週次レビューの担当者コメント・クイック入力での日次ログ | 商談詳細「商談を進める」タブ、クイック入力サイドバー |
| セールスマネージャー | 週次レビューのマネージャーコメント・ステータス付与・パイプライン俯瞰 | 商談詳細「状況を把握する」タブ、ダッシュボード、リスクレーダー |
| カスタマーサクセス/更新担当 | 契約更新の起票・更新商談の進行 | 契約更新管理、更新商談 |
| マーケティング | キャンペーン・シーケンスの設計、リード獲得経路の管理 | キャンペーン、シーケンス |
| 経営・事業責任者 | 経営レポート・スナップショット比較・組織別/商品ライン別集計 | 売上レポート、レポートビルダー、スナップショット履歴 |

実証検証では、この分担を**組織階層で明示的に表現する**ことも可能であることを確認しています(例: Segment × Business Lineの2軸・4事業部制など)。`SalesGroup`は親子関係を持てるため、任意の粒度で表現できます。

---

## 6. 外部システムとの連携インターフェース

これが**他システムと共に運用設計する際に最も重要なセクション**です。本CRM・ERP・AI_TMの3システム構成を前提に、現状の実装状況を正直に記載します。

### 6.1 全体像

```
                    ┌─────────────────────┐
                    │   AI Trade Management │  輸出コンプライアンス判定
                    │   (AI_TM, 別リポジトリ) │  制裁スクリーニング
                    └──────────┬───────────┘
                               │ ① 判定結果を受信(実装済み)
                               │ ② 審査依頼を送信(未実装)
                    ┌──────────▼───────────┐
                    │      本CRM(このリポジトリ)  │  Lead〜Contract一気通貫
                    │                        │  証跡ベースの確度スコア
                    └──────────┬───────────┘
                               │ ③ マスタをCSV取込(実装済み・手動)
                               │ ④ 受注を転記(未実装)
                    ┌──────────▼───────────┐
                    │   ERP (Mini Global ERP) │  取引先・品目マスタ
                    │   (別リポジトリ)        │  受注・出荷・請求
                    └─────────────────────┘
```

### 6.2 ERPとの連携(`erp_crm_spec.md`参照)

| 項目 | 現状 |
|---|---|
| 取引先マスタ(BusinessPartner)の取込 | **実装済み(手動CLI)** — `scripts/import_erp_business_partners_csv.py --tenant-id <uuid>`。ERPのエクスポート形状(`bp_code`/`screening_status`/`credit_limit`等)をそのまま`ErpBusinessPartner`テーブルに取り込む「箱」 |
| 品目マスタ(Material)の取込 | **実装済み(手動CLI)** — `scripts/import_erp_materials_csv.py`。`ErpMaterial`テーブル(ECCN・HSコード・外為法判定を含む) |
| ERP → CRM のリアルタイム同期(REST/Webhook) | **未実装。** `erp_crm_spec.md`§9で提案されている`bp.screening_status_changed`等のイベント受信エンドポイントは無い |
| CRM → ERP への受注転記(UC-03相当) | **未実装。** 商談クローズ時に`POST /sd/sales-orders`を呼ぶ処理は無い |
| `Account`とERPの`BusinessPartner`の紐付け | `Account.external_system`/`external_id`(汎用の外部連携キー)を`"erp"`/`bp_code`で使う設計(専用FK列は増やさない方針) |

**運用設計上の注意:** 現状は「ERPで書き出したCSVを本CRM管理者が手動で取り込む」運用です。リアルタイム同期が必要な場合は、`erp_crm_spec.md`§9の差分同期(`updated_at`ベース)またはWebhookプッシュのいずれかを別途実装する必要があります。

### 6.3 AI_TMとの連携(`CRM_INTEGRATION_HANDOVER.md`参照)

| 項目 | 現状 |
|---|---|
| コンプライアンス判定結果の受信 | **実装済み** — `POST /webhooks/compliance-judgment`(`X-Tenant-Id`ヘッダ必須)。`ComplianceStatus`をupsert |
| 制裁リスト更新時の遡及再評価 | **実装済み** — `POST /webhooks/sanctions-list-updated`。ヒットしたAccountの`ComplianceStatus`を`HIT`に更新し、進行中(非クローズ)の`Engagement`一覧を返す(実際の通知チャネルへの接続は呼び出し側の責務) |
| コンプライアンスチェックの起票 | **実装済み** — `POST /accounts/{account_id}/compliance-checks`。`ScreeningPort`経由で同期呼び出し。既定は`MockScreeningAdapter`(常にCLEAR)、`AITM_SCREENING_URL`環境変数を設定すると`AITMScreeningAdapter`が実際にAI_TMの`POST /api/screen`を呼ぶ |
| AI_TMへの取引審査登録(`POST /api/transactions`) | **未実装。** 商談成立時にAI_TMへ自動登録する処理は無い |
| `CRM_WEBHOOK_URL`(CRM側の受け口をAI_TMに登録) | 本CRM側のエンドポイントは実装済みだが、AI_TM側の`.env`設定・登録作業は別途必要 |
| 認証 | **未実装。** `webhooks.py`のコメントに明記の通り、本番投入前に署名/トークン検証を追加すること |

**環境変数(本CRM側):**
```bash
AITM_SCREENING_URL=https://screening.tsp-aitrademanagement.com
AITM_BEARER=your-aitm-api-key
```

### 6.4 本CRMが公開するJSON API一覧

画面(SSR, `/ui/...`)とは別に、外部連携用のJSON APIが並走しています(`crm_mvp/api/app.py`)。全エンドポイントで`X-Tenant-Id`ヘッダが必須です。

| メソッド | パス | 用途 |
|---|---|---|
| `POST` | `/sources` | 取り込みの唯一の入力口(トランスクリプト/録画URI/メール/自由記述) |
| `POST` | `/sources/{id}/process` | 取り込み→提案生成の同期実行(本来は非同期ワーカーの仕事) |
| `POST` | `/proposals/{id}/accept` | AI提案の承認 |
| `POST` | `/proposals/{id}/reject` | AI提案の却下(教師データとして`corrected_value`を残せる) |
| `GET` | `/engagements/{id}/gate` | ゲート判定結果・次の一手の取得 |
| `POST` | `/engagements/{id}/stage` | ステージ遷移の実行 |
| `POST` | `/engagements/{id}/waivers` | 例外承認(Waiver)の発行 |
| `GET` | `/engagements/{id}/graph` / `/graph.svg` | バイヤー相関図の取得(JSON / SVG) |
| `POST` | `/engagements/{id}/slots/{criterion}/verify` | 証拠を`VERIFIED`に昇格(人のみ) |
| `POST` | `/accounts/{id}/compliance-checks` | コンプライアンスチェックの起票(AI_TM連携、§6.3) |
| `POST` | `/webhooks/compliance-judgment` | AI_TMからの判定結果受信(§6.3) |
| `POST` | `/webhooks/sanctions-list-updated` | 制裁リスト更新の遡及再評価(§6.3) |

`Account`自体のCRUD APIは意図的に用意されていません(`HANDOVER.md`§3.10の設計方針: Account/Contact/商談ステージ・金額は既存CRM・ERPが正、というポジショニングの名残)。Account/Engagementは画面(`/ui/`)経由、またはERP CSV取込経由での作成が前提です。

### 6.5 データ所有権境界(重要 — 実態に合わせて更新)

`HANDOVER.md`§3.10の当初設計と、2026-08-14時点の実装の**乖離**を明記します。運用設計時は必ずこちらを参照してください。

| 領域 | 当初設計(`HANDOVER.md`) | **現状の実装** |
|---|---|---|
| Account / Contact | 既存CRM(読み取り＋ミラー) | **本CRムが実体を持つ。** ERPとは`external_system`/`external_id`で疎結合(専用FK無し) |
| Product / PriceBook | ERP(参照のみ、独自マスタは持たない方針) | **本CRムが独自の`Product`マスタを持つ**(CPQライク)。ERPの`ErpMaterial`とは別テーブルで、意図的に統合していない |
| Quote承認 / Contract | 自社 | 変更なし(自社) |
| リードスコアリング | 「作らない」(§6 スコープ外) | **実装されている**(`lead_scoring.py`) |
| メールシーケンス/MA機能 | 「作らない、既存CRMの領域」(§6 スコープ外) | **実装されている**(`Campaign`/`Sequence`) |
| ComplianceStatus | 自社(判定結果の保持のみ) | 変更なし。**判定ロジック自体は依然として外部(AI_TM)委譲** |
| 取引先与信・制裁スクリーニング判定 | — | **ERP/AI_TMが正。本CRムは受信・参照のみ**(`ErpBusinessPartner.screening_status`は取込CSVの値をそのまま保持、再判定はしない) |

**運用設計への含意:** 本CRムは当初の「コンパニオン」ポジショニングよりも広い範囲(商品マスタ・マーケティング機能を含む)を自前で持つに至っています。ERP/AI_TMと役割分担する際は、**「本CRムの`Product`とERPの`ErpMaterial`のどちらを正とするか」を業務要件として明示的に決める必要があります**(現状はどちらも並行して存在し、自動的な整合性チェックはありません)。

---

## 7. 運用設計上の注意点

1. **本番投入前に認証層を追加すること。** 現状はCookie/ヘッダによる自己申告のテナント識別のみです。少なくともリバースプロキシでのBasic認証やVPN限定アクセスを設定してください。
2. **`case_no`/`bp_code`等のビジネスキーの命名規則を統一すること。** `CRM_INTEGRATION_HANDOVER.md`§4が推奨するように、CRM側の商談番号は`CRM-{id}`形式にするなど、ERP・AI_TM側の重複登録防止ルールと整合させてください。
3. **通貨は集計時に自動換算されません。** 複数通貨のテナントでは、経営レポート・リスクレーダーは通貨ごとに分けて表示されます。連結会計が必要な場合は別途換算レイヤーを設計してください。
4. **Webhookの送信元検証は未実装です。** `POST /webhooks/*`は`X-Tenant-Id`ヘッダのみで宛先を決めており、送信元の真正性を検証していません。本番前に署名検証を追加してください。
5. **ERP同期は現状バッチ(手動CLI)です。** リアルタイム性が必要な業務(与信枠のリアルタイム参照など)がある場合は、`erp_crm_spec.md`§9の連携方式(ポーリング/Webhook/MQ)から選定し別途実装してください。
6. **`Product`と`ErpMaterial`の二重管理に注意。** §6.5参照。どちらを正とするか、または統合するかを業務要件として決定してください。

---

## 8. 用語集

| 用語 | 意味 |
|---|---|
| Engagement | 商談。本CRムの中心オブジェクト |
| QualificationSlot | 商談の評価軸1件分(主張+根拠+証拠強度) |
| Confidence(ASSERTED/CORROBORATED/VERIFIED) | 証拠強度の3段階。VERIFIEDは人しか付与できない |
| GatePolicy / GateEvaluation | ステージ移行を許可するための条件定義とその評価結果 |
| Waiver | ゲートをブロックされた場合の例外承認 |
| relationship_type(renewal/upsell/cross_sell) | 商談の親子関係の種別。契約更新・拡張販売の表現に使う |
| ReportSnapshot | 任意時点の経営指標(売上・キャンペーン・シーケンス・パイプライン)を固定化した記録 |
| as_of | 集計関数に過去日付を渡し、その時点の実履歴から状態を再構成するパラメータ |
| ScreeningPort | 外部スクリーニング(反社・与信・輸出管理)を呼び出す抽象インターフェース。既定はモック |
| ErpBusinessPartner / ErpMaterial | ERPマスタをそのまま受け止める「箱」テーブル。CRM独自オブジェクトとは別物 |

---

## 9. 関連ドキュメント

- `HANDOVER.md` — 本CRムの設計思想・不変条件・実装ロードマップ(一部当初計画のまま、§0参照)
- `CRM_INTEGRATION_HANDOVER.md` — AI_TM側のAPI仕様・Webhook設計の一次情報
- `erp_crm_spec.md` — ERP側のデータモデル・API仕様の一次情報
- `README.md` — パッケージ構成・人とAIの入力分担の要約
