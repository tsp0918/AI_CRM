# ERP システム仕様書（CRM連携用）

**文書バージョン:** 1.0  
**発行日:** 2026-08-11  
**ステータス:** DRAFT  
**ERP Version:** 0.1.0  
**Base URL:** `http://localhost:8888`

---

## 目次

1. [文書概要](#1-文書概要)
2. [ERPシステム概要](#2-erpシステム概要)
3. [接続・認証仕様](#3-接続認証仕様)
4. [取引先マスタ（BusinessPartner）](#4-取引先マスタbusinesspartner)
5. [制裁スクリーニング](#5-制裁スクリーニング)
6. [品目マスタ（Material）](#6-品目マスタmaterial)
7. [受注・出荷・請求ドキュメント](#7-受注出荷請求ドキュメント)
8. [CRM連携ユースケース（案）](#8-crm連携ユースケース案)
9. [Webhook・イベント連携](#9-webhookイベント連携)
10. [付録：ステータスコード一覧](#10-付録ステータスコード一覧)

---

## 1. 文書概要

本書は Mini Global ERP（以下「ERP」）と今後開発するCRMシステムとのシステム間連携を設計・実装するための引き継ぎ書です。CRM開発チームがERP側の仕様を理解し、インターフェース設計の要件定義を進める際の一次情報として使用してください。

**現時点の位置づけ:** 連携インターフェースの実装は未実施。本書はERP側の現行仕様の共有が目的です。実際の連携方式（REST API同期、イベントドリブン、バッチ連携など）は本書を踏まえた要件定義フェーズで決定します。

### 主な連携対象データ

| データ領域 | ERP側オブジェクト | 連携方向（案） | 優先度 |
|---|---|---|---|
| 取引先情報 | BusinessPartner（BP） | ERP → CRM（マスタ同期） | 高 |
| 制裁スクリーニング結果 | screening_status / denial_list | ERP → CRM（ステータス通知） | 高 |
| 受注サマリー | SalesOrder | ERP → CRM（参照） | 中 |
| 与信枠・支払条件 | credit_limit / payment_terms | 双方向（要検討） | 中 |
| 品目情報 | Material | ERP → CRM（カタログ参照） | 低 |
| 請求履歴 | BillingDocument | ERP → CRM（参照） | 低 |

---

## 2. ERPシステム概要

Mini Global ERP は、半導体製造材料・装置の輸出取引に特化した業務ERPです。取引先管理・販売管理・調達・製造・財務・品質管理の各機能を持ち、AI Trade Management（AI_TM）と連携して輸出コンプライアンスを自動審査します。

### 技術スタック

| レイヤ | 技術 | 備考 |
|---|---|---|
| APIフレームワーク | FastAPI (Python) | OpenAPI 3.1 / Swagger UI: `/docs` |
| ORM / DB | SQLAlchemy + SQLite | 本番移行時はPostgreSQLを推奨 |
| 認証 | OAuth2 Password Grant + JWT Bearer | HS256、有効期限60分 |
| テナント分離 | `client_id` カラムによる論理分離 | 現在は `DEMO` テナントのみ |
| 外部連携 | AI_TradeManagement（マイクロサービス群） | 現在MOCKモードで動作 |

### モジュール構成

| コード | 名称 | CRM連携 |
|---|---|---|
| **MDM** | マスタデータ管理（取引先・品目・会社） | ★ 主要 |
| **SD** | 販売管理（受注・出荷・請求） | ★ 主要 |
| **GTS** | 貿易コンプライアンス（制裁・輸出申告） | ★ 主要 |
| PP | 生産管理（製造指図・レシピ・工程） | — |
| MM | 購買・在庫管理 | — |
| FI | 財務会計（仕訳・GL） | — |
| QM | 品質管理（検査ロット・CoA） | — |
| CO | 管理会計（コストセンター・原価） | — |
| HR | 人事管理（社員・部門） | — |

### 現在のデータ量（参考）

| オブジェクト | 件数 | 内訳 |
|---|---|---|
| BusinessPartner | 111 | CLEARED:94、BLOCKED:13、FLAGGED:4 |
| Material | 72 | ROH:27、FERT:26、HALB:13、HAWA:6 |
| SalesOrder | 258 | AI_TM審査リンク99件（APPROVED:77、PENDING:8、REJECTED:5、BLOCKED:9） |
| Delivery / Billing | 各110 | SDドキュメントチェーン済み |
| ExportDeclaration | 256 | 輸出申告・許可証管理 |
| DeniedPartyScreeningLog | 248 | 制裁スクリーニング監査ログ |

---

## 3. 接続・認証仕様

### エンドポイント情報

| 項目 | 値 |
|---|---|
| Base URL（開発） | `http://localhost:8888` |
| APIリファレンス | `http://localhost:8888/docs`（Swagger UI） |
| OpenAPI JSON | `http://localhost:8888/openapi.json` |
| 認証方式 | OAuth2 Password Grant → JWT Bearer Token |
| トークン有効期限 | 60分 |
| テナント識別子 | `client_id=DEMO`（固定） |

### トークン取得

```
POST /auth/token
Content-Type: application/x-www-form-urlencoded

username=admin@example.com&password=admin1234&client_id=DEMO

──── レスポンス ────
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

### リクエストヘッダー

```
Authorization: Bearer {access_token}
Content-Type: application/json
```

> **CRM連携時の認証設計について：**  
> 現行のERP認証はユーザー名＋パスワードのパスワードグラントです。CRM-ERP間のシステム間連携では、**専用のサービスアカウント発行** または **Client Credentials Grant方式への移行** を要件定義フェーズで検討してください。

### ページネーション（全リストAPI共通）

| パラメータ | 説明 |
|---|---|
| `?limit=N` | 1ページあたりの件数 |
| `?offset=N` | スキップ件数（0始まり） |
| レスポンス形式 | `{"items": [...], "total": N}` |

### エラーレスポンス形式

```json
// バリデーションエラー (422)
{ "detail": [{"type": "missing", "loc": ["query","material_code"], "msg": "Field required"}] }

// ビジネスロジックエラー (404等)
{ "detail": "Material not found: MAT-9999", "type": "NotFoundError" }
```

---

## 4. 取引先マスタ（BusinessPartner）

ERPのBusinessPartner（BP）はSAP S/4HANAのBP統合モデルに準じており、**顧客・仕入先・従業員を1レコードで統合管理**します。CRMの取引先エンティティはこのBPを正とし、`bp_code` をキーに参照してください。

> **設計上の重要点：** ERPがマスタです。BPの新規登録はERP側で行い（またはCSV一括インポート）、CRMへの同期はERP→CRM方向が基本です。

### フィールド定義

| フィールド名 | 型 | I/O | 説明 |
|---|---|---|---|
| `bp_code` | string(20) | 参照 | 取引先コード（`BP-XXXXXXX`形式、自動採番）。**CRM連携キー** |
| `bp_type` | enum | 任意 | `ORG`（法人）/ `PERSON`（個人） |
| `name` | string(255) | **必須** | 取引先名称 |
| `country` | string(2) | **必須** | 国コード（ISO 3166-1 alpha-2）例: `JP` `US` `TW` `KR` |
| `roles` | string | **必須** | 役割（カンマ区切り複数可）: `CUSTOMER` / `VENDOR` / `EMPLOYEE` |
| `email` | string(255) | 任意 | メールアドレス |
| `phone` | string(50) | 任意 | 電話番号 |
| `address_line1` | string(255) | 任意 | 住所1 |
| `address_line2` | string(255) | 任意 | 住所2（建物名等） |
| `city` | string(100) | 任意 | 市区町村 |
| `postal_code` | string(20) | 任意 | 郵便番号 |
| `credit_limit` | decimal(18,2) | 任意 | 与信限度額（`currency` フィールドの通貨） |
| `payment_terms` | string(20) | 任意 | 支払条件: `NET30` `NET60` `NET90` など |
| `currency` | string(3) | 任意 | 取引通貨（ISO 4217）: `USD` `JPY` `EUR` |
| `is_denied_party` | boolean | **参照のみ** | 制裁対象フラグ（`screening_status=BLOCKED` 時に `true`） |
| `screening_status` | enum | **参照のみ** | スクリーニング結果（§5 参照） |
| `denial_list` | string(100) | **参照のみ** | 一致した制裁リスト名: `BIS_ENTITY_LIST` `OFAC_SDN` など |
| `denial_reason` | string(500) | **参照のみ** | 制裁理由（自由テキスト） |
| `last_screened_at` | datetime | **参照のみ** | 最終スクリーニング実施日時（ISO 8601） |
| `fifty_pct_rule_triggered` | boolean | **参照のみ** | OFAC 50%ルール適用フラグ |
| `parent_sanctioned_entity` | string(255) | **参照のみ** | 50%ルール適用時の親制裁対象企業名 |
| `ai_tm_screening_ref` | string(100) | **参照のみ** | AI_TM スクリーニング参照番号（監査トレース用） |
| `is_active` | boolean | 任意 | 有効フラグ（`false` = 論理削除済み） |
| `created_at` | datetime | **参照のみ** | レコード作成日時 |
| `updated_at` | datetime | **参照のみ** | 最終更新日時。**変更検知の差分同期に使用** |

*I/O凡例: 必須（登録時）/ 任意（省略可）/ 参照のみ（ERPが自動設定、CRMからの書き込み不可）*

### APIエンドポイント

```
GET    /mdm/business-partners              一覧取得（ページネーション対応）
GET    /mdm/business-partners/{item_id}   1件取得（内部ID指定）
POST   /mdm/business-partners             新規登録（auto_screen=true で自動スクリーニング）
PUT    /mdm/business-partners/{item_id}   更新（name/country/roles/与信枠等）
POST   /import/business-partners          CSVファイル一括インポート
```

### レスポンスサンプル（GET /mdm/business-partners?limit=2）

```json
{
  "items": [
    {
      "bp_code": "BP-1000001",
      "bp_type": "ORG",
      "name": "NSC Electronic Materials USA, Inc.",
      "country": "US",
      "roles": "CUSTOMER",
      "email": "procurement@nsc-usa.com",
      "city": "San Jose",
      "credit_limit": "5000000.00",
      "payment_terms": "NET30",
      "currency": "USD",
      "is_denied_party": false,
      "screening_status": "CLEARED",
      "denial_list": null,
      "denial_reason": null,
      "last_screened_at": "2026-07-01T09:15:00.000000",
      "fifty_pct_rule_triggered": false,
      "parent_sanctioned_entity": null,
      "ai_tm_screening_ref": "MOCK-EVT-a1b2c3d4",
      "is_active": true,
      "created_at": "2026-05-03T09:36:28.651985",
      "updated_at": "2026-07-01T09:15:01.123456"
    },
    {
      "bp_code": "BP-2000007",
      "name": "Huawei Technologies Co., Ltd.",
      "country": "CN",
      "roles": "CUSTOMER,VENDOR",
      "is_denied_party": true,
      "screening_status": "BLOCKED",
      "denial_list": "BIS_ENTITY_LIST",
      "denial_reason": "BIS Entity List (EAR §744.11): License required for all items",
      "last_screened_at": "2026-07-01T09:16:22.000000",
      "fifty_pct_rule_triggered": false,
      "is_active": true
    }
  ],
  "total": 111
}
```

### 新規登録リクエストサンプル

```json
POST /mdm/business-partners

{
  "name": "ABC Semiconductor Pte. Ltd.",
  "country": "SG",
  "roles": "CUSTOMER",
  "email": "contact@abc-semi.sg",
  "city": "Singapore",
  "credit_limit": "2000000.00",
  "payment_terms": "NET60",
  "currency": "USD",
  "auto_screen": true
}
```

`auto_screen: true` にすると登録直後にAI_TMスクリーニングが自動実行され、`screening_status` が設定されます。

---

## 5. 制裁スクリーニング

ERPはすべてのBPに対してAI Trade Managementと連携した**自動制裁スクリーニング**を実施します。

> **CRM側での取り扱い：** `screening_status = BLOCKED` の取引先は法的に取引が制限されている可能性があります。CRM上で商談・見積を作成する際はERP側のステータスを必ず確認し、BLOCKED先への案件進行を抑制する設計を推奨します。最終的な取引可否はコンプライアンス部門が判断します。

### screening_status 値定義

| ステータス | 意味 | CRMでの推奨アクション |
|---|---|---|
| `UNSCREENED` | 未スクリーニング（新規登録直後等） | スクリーニング実施を促す警告を表示 |
| `CLEARED` | 審査通過（制裁リストへの該当なし） | 通常通り商談・受注処理を進める |
| `FLAGGED` | 要注意（OFAC 50%ルール対象 or WATCHLIST該当） | コンプライアンス担当者へのエスカレーション警告を表示 |
| `BLOCKED` | 取引禁止（BIS Entity List / OFAC SDN直接掲載確認済み） | 受注・見積の作成をブロック、管理者承認フローを強制 |

### 適用される制裁リスト

| denial_list 値 | 根拠法令・規制 | 結果ステータス |
|---|---|---|
| `BIS_ENTITY_LIST` | 米国輸出管理規則（EAR）§744.11 BIS Entity List | BLOCKED |
| `OFAC_SDN` | OFAC Specially Designated Nationals List（米国財務省制裁） | BLOCKED |
| `OFAC_50PCT` | OFAC 50%ルール（SDN掲載企業が50%以上保有する法人） | FLAGGED |
| `WATCHLIST` | 独自ウォッチリスト（COSCO一部子会社、イラン再輸出懸念先等） | FLAGGED |
| `COUNTRY_EMBARGO` | 禁輸国（IR・KP・SY・CU）への取引 | BLOCKED |
| `ELEVATED_RISK` | 高リスク国（RU・BY・VE・MM）への取引 | FLAGGED |

### スクリーニング関連API

```
GET  /gts/screening/log                     スクリーニング監査ログ一覧
POST /gts/screening/{bp_code}/rescreen      特定BPの再スクリーニング実行
POST /gts/screening/rescreen-all            全BP一括再スクリーニング（?only_unscreened=true も可）
```

### スクリーニングログフィールド（監査用）

| フィールド | 型 | 説明 |
|---|---|---|
| `bp_code` | string | 対象取引先コード |
| `match_status` | enum | `no_match` / `possible_match` / `match` / `CRITICAL` |
| `match_score` | decimal(5,2) | マッチ確度（0.00〜1.00） |
| `matched_list` | string | 一致したリスト名 |
| `matched_entity_name` | string | 一致したエンティティ名（制裁リスト上の正式名称） |
| `fifty_pct_rule_triggered` | boolean | 50%ルール適用の有無 |
| `parent_sanctioned_entity` | string | 親制裁対象企業名 |
| `ownership_pct` | decimal(5,1) | 推定保有比率（%） |
| `screened_at` | datetime | スクリーニング実施日時 |
| `screened_by` | string | 実施者（`system` / ユーザーメール） |

---

## 6. 品目マスタ（Material）

半導体製造材料・装置類を中心とした品目マスタです。CRMは主に製品カタログ表示・見積品目の選択用途で参照します。

### 品目タイプ（material_type）

| コード | 名称 | 例 |
|---|---|---|
| `FERT` | 完成品（製品） | ArFフォトレジスト、EUVフォトレジスト、CMP装置 |
| `HALB` | 半製品（中間体） | PAGポリマー溶液、レジストベースポリマー |
| `ROH` | 原材料 | PGMEA溶媒、HF酸、シリコン前駆体、PAGモノマー |
| `HAWA` | 商品（購入品・転売） | ウェーハ搬送カセット、クリーンルーム消耗品 |

### CRM参照向けフィールド

| フィールド名 | 型 | 説明 |
|---|---|---|
| `material_code` | string(20) | 品目コード（`MAT-XXXXXXX`形式）。CRM見積の品目キー |
| `description` | string(255) | 品目説明文 |
| `material_type` | enum | FERT / HALB / ROH / HAWA |
| `base_unit` | string(5) | 基本単位（`KG` `L` `PC` `SET`） |
| `standard_price` | decimal(15,2) | 標準価格 |
| `currency` | string(3) | 価格通貨 |
| `hs_code` | string(20) | HSコード（関税分類番号）例: `3707.90` |
| `eccn` | string(20) | 米国輸出規制分類番号。例: `3B001` `EAR99`。**CRM商談の輸出審査判断に使用** |
| `fefta_judgment` | enum | 外為法判定: `APPROVED` / `APPLICABLE` / `UNKNOWN` / `NOT_APPLICABLE` |
| `country_of_origin` | string(2) | 原産国（ISO 3166-1 alpha-2）。米国EAR De Minimis計算に影響 |
| `is_active` | boolean | 有効フラグ |

> **ECCN・HSコードの利用について：**  
> ECCN が `3B001`（半導体製造装置）・`3C001`（シリコン前駆体）等の輸出規制対象品目は、仕向国によっては個別ライセンスが必要です。CRM見積・商談作成時に品目のECCNと取引先の国コードの組み合わせをチェックし、コンプライアンスレビューを促す設計を推奨します。

### APIエンドポイント

```
GET /mdm/materials              品目一覧（ページネーション対応）
GET /mdm/materials/{item_id}   1件取得
```

---

## 7. 受注・出荷・請求ドキュメント

ERPのSDモジュールは、受注（SalesOrder）→ 出荷（Delivery）→ 請求（BillingDocument）のドキュメントチェーンで販売プロセスを管理します。

**ドキュメントチェーン:**  
`[CRM商談]` → `SalesOrder (001XXXXXXX)` → `Delivery (DEL-XXXXXXX)` → `Billing (BIL-XXXXXXX)` → `ExportDeclaration (EXP-XXXXXXX)`

### 受注（SalesOrder）フィールド定義

| フィールド名 | 型 | 説明 |
|---|---|---|
| `document_number` | string | 受注番号（10桁数字文字列） |
| `document_date` | date | 受注日 |
| `status` | enum | ドキュメントステータス（下記参照） |
| `customer_code` | string(20) | 顧客BPコード（`bp_code`） |
| `customer_po_number` | string(50) | 顧客注文番号（顧客側の発注番号） |
| `requested_delivery_date` | date | 希望納期 |
| `incoterms` | string(10) | インコタームズ: `FOB` `CIF` `EXW` `DDP` など |
| `payment_terms` | string(20) | 支払条件 |
| `currency` | string(3) | 受注通貨（主に `USD`） |
| `total_amount` | decimal(18,2) | 受注総額 |
| `export_check_status` | enum | 輸出審査ステータス（下記参照） |
| `export_check_ref` | string(50) | AI_TM 輸出審査参照番号 |
| `items[]` | array | 受注明細（material_code / quantity / unit / unit_price / net_amount） |

#### SalesOrder ステータス

| status | 意味 |
|---|---|
| `OPEN` | 受注登録済み・処理待ち |
| `RELEASED` | リリース済み（出荷可能） |
| `COMPLETED` | 完了（請求発行済み） |
| `BLOCKED` | ブロック（輸出審査不可等） |
| `CANCELLED` | キャンセル済み |

#### export_check_status（輸出審査ステータス）

| 値 | 意味 |
|---|---|
| `PENDING` | AI_TM 審査待ち |
| `PASSED` | 輸出審査通過 |
| `BLOCKED` | 輸出不可（制裁対象企業・品目・仕向国） |
| `SKIPPED` | 審査スキップ（`skip_export_check=true` で登録） |

### 出荷（Delivery）・請求（Billing）

| フィールド名 | オブジェクト | 説明 |
|---|---|---|
| `document_number` | Delivery / Billing | 出荷番号 / 請求番号 |
| `sales_order_id` | Delivery / Billing | 元受注の内部ID |
| `actual_delivery_date` | Delivery | 実際の出荷日 |
| `aitm_approval_status` | Delivery | AI_TM出荷前再スクリーニング結果（PENDING / APPROVED / BLOCKED） |
| `net_amount / tax_amount / gross_amount` | Billing | 税抜額 / 税額 / 税込総額 |
| `payment_terms` | Billing | 請求の支払条件 |

### SDモジュール APIエンドポイント

```
GET  /sd/sales-orders                  受注一覧
GET  /sd/sales-orders/{so_id}          受注1件取得（明細含む）
POST /sd/sales-orders                  受注新規登録（輸出審査自動実行）
POST /sd/sales-orders/{so_id}/release  受注リリース
GET  /sd/deliveries                    出荷一覧
GET  /sd/billing                       請求一覧
GET  /sd/billing/{billing_id}/pdf      請求書PDF ダウンロード
GET  /sd/forecasts/summary?year=2026   月次フォーキャスト vs 実績サマリー（year必須）
```

---

## 8. CRM連携ユースケース（案）

### UC-01: 取引先マスタの初期同期・定期同期

ERP の BusinessPartner をCRM の取引先マスタへ同期する。

1. `GET /mdm/business-partners?limit=100&offset=0` でページネーションしながら全件取得
2. `bp_code` をERP-CRM間の名寄せキーとして使用
3. 定期同期は `updated_at` による差分同期を推奨（全件同期はデータ量増加時に非効率）

### UC-02: 制裁スクリーニング結果のCRM反映

ERP側でスクリーニングステータスが変更された際に、CRM側の取引先ステータスを更新する。

1. ERP側でスクリーニング実行（BP登録時・定期バッチ・手動再スクリーニング）
2. `screening_status` が BLOCKED / FLAGGED に変わった際にCRMへ通知（Webhook案：§9参照）
3. CRMは BLOCKED 取引先への新規商談・見積作成を抑制するか警告を表示

### UC-03: CRM商談からERP受注への転記

CRMで成約した商談をERP受注に転記する（CRM → ERP 方向）。

1. CRM商談のクローズ後、ERP の `POST /sd/sales-orders` を呼び出す
2. リクエストに `customer_code`（bp_code）・品目・数量・単価・通貨を含める
3. `skip_export_check=false`（デフォルト）でAI_TM輸出審査が自動実行される
4. ERP側で採番された `document_number` をCRM商談にフィードバックして紐付け

### UC-04: 与信枠の参照・更新

CRM の営業担当者が取引先の与信状況をリアルタイムで確認する。

1. `GET /mdm/business-partners/{id}` で `credit_limit` を取得
2. 受注残高は `GET /sd/sales-orders?customer_code=BP-XXXXXXX` から集計
3. 与信枠変更は `PUT /mdm/business-partners/{id}` で `credit_limit` を更新（承認ワークフローは要件定義で検討）

### UC-05: CRM商談でのECCN・輸出規制チェック

CRM上で見積品目と仕向国の組み合わせによる輸出規制リスクを事前に確認する。

1. CRM見積作成時に `GET /mdm/materials/{id}` で `eccn` を取得
2. 取引先の `country` と `eccn` の組み合わせで規制要否を表示（ECCNルックアップ表はCRM側で保持）
3. 規制対象の場合は「輸出ライセンス要確認」の警告をCRM上で表示し、受注転記前にコンプライアンス確認を要求

---

## 9. Webhook・イベント連携

### ERP側の既存Webhookエンドポイント（受信）

現在ERPはAI_TradeManagementからの通知を受信するWebhookを実装しています。CRMへの通知エンドポイントは未実装であり、要件定義フェーズで設計します。

```
POST /gts/webhook/judgment-updated   AI_TMからの輸出審査結果受信（ERP受信専用）
```

### CRM連携で今後検討するERPイベント（案）

| イベント名 | トリガー | CRMへの通知内容（案） |
|---|---|---|
| `bp.screening_status_changed` | BPの `screening_status` が変更された時 | bp_code / 旧ステータス / 新ステータス / denial_list / screened_at |
| `sales_order.export_blocked` | 受注の `export_check_status` が `BLOCKED` になった時 | document_number / customer_code / block_reason |
| `sales_order.approved` | 受注が `RELEASED` ステータスに移行した時 | document_number / customer_code / total_amount |
| `billing.issued` | 請求書が発行された時 | billing_number / customer_code / gross_amount |
| `bp.credit_limit_changed` | 与信枠が変更された時 | bp_code / old_limit / new_limit / changed_by |

### 連携方式の選択肢

**① REST ポーリング**  
CRM が定期的に `GET /mdm/business-partners?updated_after=TIMESTAMP` 等で差分を取得。ERP側の改修が少なく済む。

**② Webhook プッシュ**  
ERP がイベント発生時にCRMのエンドポイントをPOST呼び出し。リアルタイム性が高い。ERP側にWebhookディスパッチャーの実装が必要。

**③ メッセージキュー（Kafka/SQS等）**  
高可用性・スケーラビリティが必要な場合。インフラ整備コストが高い。

現在のERPアーキテクチャ（FastAPI + SQLite）では①または②が実装コスト的に現実的です。

---

## 10. 付録：ステータスコード一覧

### HTTPステータスコード

| コード | 意味 | 対処 |
|---|---|---|
| 200 | OK | 正常 |
| 201 | Created | リソース作成成功 |
| 400 | Bad Request | リクエスト形式不正。`detail`を確認 |
| 401 | Unauthorized | トークン未提供または期限切れ。再取得すること |
| 403 | Forbidden | 権限不足 |
| 404 | Not Found | 指定IDが存在しない |
| 409 | Conflict | 重複キー（同一コードが既存） |
| 422 | Unprocessable Entity | バリデーションエラー。必須パラメータ欠損等 |
| 500 | Internal Server Error | ERP側エラー |

### ステータスコード一覧まとめ

| フィールド | 取りうる値 |
|---|---|
| BP: `screening_status` | `UNSCREENED` / `CLEARED` / `FLAGGED` / `BLOCKED` |
| BP: `roles` | `CUSTOMER` / `VENDOR` / `EMPLOYEE`（カンマ区切り複合可） |
| BP: `bp_type` | `ORG` / `PERSON` |
| Material: `material_type` | `FERT` / `HALB` / `ROH` / `HAWA` |
| Material: `fefta_judgment` | `UNKNOWN` / `NOT_APPLICABLE` / `APPLICABLE` / `APPROVED` / `PENDING` |
| SalesOrder: `status` | `OPEN` / `RELEASED` / `COMPLETED` / `BLOCKED` / `CANCELLED` |
| SalesOrder: `export_check_status` | `PENDING` / `PASSED` / `BLOCKED` / `SKIPPED` |
| Delivery: `aitm_approval_status` | `PENDING` / `APPROVED` / `BLOCKED` / `ERROR` |
| ExportDeclaration: `status` | `DRAFT` / `SUBMITTED` / `APPROVED` / `REJECTED` / `CANCELLED` |
| AITMTransactionLink: `review_status` | `PENDING` / `APPROVED` / `REJECTED` / `BLOCKED` |

### 主要コード体系

| コード種別 | 形式 | 例 |
|---|---|---|
| 取引先コード | `BP-XXXXXXX`（7桁数字） | `BP-1000001` |
| 品目コード | `MAT-XXXXXXX`（7桁数字） | `MAT-1000001` |
| 受注番号 | 10桁数字文字列 | `0010000000` |
| 出荷番号 | `DEL-XXXXXXX` | `DEL-0000001` |
| 請求番号 | `BIL-XXXXXXX` | `BIL-0000001` |
| 輸出申告番号 | `EXP-XXXXXXX` | `EXP-0000001` |
| AI_TM参照番号 | `MOCK-EVT-XXXXXXXX`（MOCKモード） | `MOCK-EVT-a1b2c3d4` |

---

> **本書の管理について：**  
> 本書は ERP Version 0.1.0（2026-08-11時点）のスナップショットです。ERPのAPI変更・モデル変更が発生した場合は本書を更新してください。最新の完全なAPI定義は常に `GET /openapi.json` または `/docs`（Swagger UI）を参照してください。
