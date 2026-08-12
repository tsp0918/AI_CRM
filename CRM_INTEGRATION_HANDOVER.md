# AI Trade Management — CRM 連携 引き継ぎ書

作成日: 2026-08-11  
ステータス: DRAFT / Internal

---

## 目次

1. [ドキュメント概要](#1-ドキュメント概要)
2. [システム全体構成](#2-システム全体構成)
3. [CRM 連携 設計コンセプト](#3-crm-連携-設計コンセプト)
4. [ERP・CRM データ分担](#4-erpcrm-データ分担)
5. [取引審査 API（ai_validation :8011）](#5-取引審査-apiai_validation-8011)
6. [制裁スクリーニング API（screening :8005）](#6-制裁スクリーニング-apiscreening-8005)
7. [品目管理 API（ai_classification :8002）](#7-品目管理-apiai_classification-8002)
8. [R&D リスク管理 API（rnd_assessment :8003）](#8-rd-リスク管理-apirnd_assessment-8003)
9. [該非判定共有 DB（platform-core /api/hantei）](#9-該非判定共有-dbplatform-core-apihantei)
10. [プラットフォーム共通 API（platform-core :8000）](#10-プラットフォーム共通-apiplatform-core-8000)
11. [Webhook / イベント設計](#11-webhook--イベント設計)
12. [CRM フィールドマッピング](#12-crm-フィールドマッピング)
13. [データフロー](#13-データフロー)
14. [認証・環境設定](#14-認証環境設定)
15. [クイックスタート](#15-クイックスタート)
16. [実装ロードマップ](#16-実装ロードマップ)
17. [注意事項・制約](#17-注意事項制約)

---

## 1. ドキュメント概要

本ドキュメントは、AI Trade Management（AI_TM）システムと新規 CRM（Customer Relationship Management）システムを連携させる際に参照する技術仕様書です。
CRM 開発チームが AI_TM のデータ構造・API・イベントフローを正確に把握し、スムーズな統合設計を行えることを目的とします。

**スコープ:** 本書は「CRM 側から AI_TM を参照・連携する」観点で記述します。AI_TM 内部のビジネスロジックの詳細は `CLAUDE.md` および各モジュールの `README` を参照してください。

| 項目 | 内容 |
|------|------|
| AI_TM 稼働環境 | macOS / Python 3.12 / FastAPI / PostgreSQL + SQLite / FAISS |
| 外部公開ベース URL | `https://app.tsp-aitrademanagement.com`（Cloudflare Tunnel） |
| ERP 連携状況 | 既存連携あり（localhost:8888）— 受注連動・判定通知・De Minimis 検知 |
| CRM 連携状況 | 未実装（本書が設計起点） |
| 認証方式 | Bearer トークン（環境変数で管理） |

---

## 2. システム全体構成

AI_TM は独立した FastAPI マイクロサービス群で構成されます。各モジュールはポートで分離され、内部通信は `MODULE_*_URL` 環境変数、外部（ブラウザ/CRM）からは Cloudflare Tunnel 経由でアクセスします。

| モジュール | ポート | DB | 役割 | CRM 関連度 |
|---|---|---|---|---|
| platform-core | 8000 | PostgreSQL | 共通基盤・FAISS・知識グラフ・規制スケジューラー・Agent | 中（該非判定DB参照） |
| ai_validation | 8011 | SQLite | 取引審査（ECCN/外為法判定・スクリーニング連動） | ★★★ 主要接点 |
| ai_classification | 8002 | SQLite | 品目管理・ECCN分類・該非判定・HS分類・国別規制プロファイル | ★★ 品目照会 |
| rnd_assessment | 8003 | SQLite | R&Dリスク評価・みなし輸出・人物管理 | ★★ 案件起点参照 |
| screening | 8005 | PostgreSQL | 制裁リストスクリーニング（OFAC/BIS/EU/UN/UK — 55,093件+） | ★★★ 顧客登録連動 |
| patent_search | 8004 | SQLite | 特許検索（BigQuery + J-PlatPat） | 低 |
| hs_classifier | 8006 | FAISS | HSコード判定（5,476vec） | 低（間接利用） |
| dap | 8010 | SQLite | AIオーケストレーター（Claude API）— 会話型コンプライアンス支援 | 中（Phase 5） |

**外部公開ドメイン（Cloudflare Tunnel）:**

| モジュール | 外部 URL |
|---|---|
| platform-core | `https://app.tsp-aitrademanagement.com` |
| ai_validation | `https://validation.tsp-aitrademanagement.com` |
| ai_classification | `https://classification.tsp-aitrademanagement.com` |
| rnd_assessment | `https://rnd.tsp-aitrademanagement.com` |
| screening | `https://screening.tsp-aitrademanagement.com` |
| patent_search | `https://patent.tsp-aitrademanagement.com` |
| hs_classifier | `https://hs.tsp-aitrademanagement.com` |
| dap | `https://dap.tsp-aitrademanagement.com` |

---

## 3. CRM 連携 設計コンセプト

CRM が AI_TM と連携する主な目的は以下の 3 点です。

### ① 輸出コンプライアンス判定を案件情報に組み込む

CRM の「取引案件」に対し、AI_TM が行う輸出規制判定結果（ECCN 該否・スクリーニング・輸出許可要否）を自動連動させる。営業担当者が CRM を操作するだけでコンプライアンスチェックが走る構造。

### ② R&D 段階から取引審査まで案件データを引き継ぐ

R&D → 品目管理 → 取引審査の判定データ継承が AI_TM 内部で実装済み。CRM からは `case_no` や `product_code` をキーに過去の判定履歴を参照できる。

### ③ 取引先スクリーニングを CRM 顧客マスタと連動させる

OFAC/BIS 等の制裁リスト照合（55,093件+）を CRM の顧客・取引先登録フローに組み込む。登録・更新時に自動スクリーニングを発火し、結果を CRM へ通知する。

---

## 4. ERP・CRM データ分担

既存の ERP 連携との役割分担を明確にします。

| データ種別 | ERP（既存） | CRM（新規） | AI_TM 側フィールド |
|---|---|---|---|
| 受注番号 | ERP 主管 | — | `erp_case_no` |
| 取引案件番号 | — | CRM 主管 | `case_no`（CRM 指定 or AI_TM 自動採番） |
| 取引先名 | ERP 顧客マスタ | CRM 顧客マスタ | `counterparty_name` |
| 品目コード・ECCN | 品目マスタ（ERP サイロ） | 参照のみ | `linked_product_code` / `linked_product_eccn` |
| 仕向地・最終需要者 | 出荷管理 | CRM 案件情報 | `destination_country` / `end_user_name` |
| 取引金額・数量 | 受注金額 | 見積・案件金額 | `total_value_usd` / `quantity` |
| コンプライアンス判定結果 | ERP → AI_TM Webhook 受信 | CRM → AI_TM Webhook 受信 | `status`（APPROVED/REJECTED/PENDING） |
| 制裁スクリーニング結果 | — | CRM 顧客登録時に連動 | `screening_status` / `result_status` |
| R&D 連携・みなし輸出 | — | CRM 提案段階で参照 | `rnd_case_id` |

> **重複登録に注意:** ERP が `erp_case_no` で取引を登録した案件に対し、CRM が別途 `case_no` で重複登録しないよう、ビジネスキーの命名規則を統一してください。推奨: CRM 案件番号 = `CRM-{crm_id}` の形式で `case_no` フィールドに指定。

---

## 5. 取引審査 API（ai_validation :8011）

CRM から最も頻繁に利用するモジュール。取引案件の輸出規制判定を管理します。

### 5-1. 取引審査の新規登録

CRM の案件成立時またはコンプライアンスチェック開始時に呼び出します。

```
POST https://validation.tsp-aitrademanagement.com/api/transactions
Authorization: Bearer {CRM_BEARER_TOKEN}
Content-Type: application/json

{
  "title":             "半導体テスト装置 — 中国向け案件 #2026-CRM-001",
  "case_no":           "CRM-2026-001",          // CRM の案件番号（省略時は自動採番）
  "counterparty_name": "CXMT Co., Ltd.",
  "destination_country": "CN",                  // ISO 3166-1 alpha-2
  "end_user_name":     "長鑫存储技術有限公司",
  "end_user_country":  "CN",
  "end_use_description": "メモリウェハ製造ライン向け検査工程",
  "product_code":      "SEMI-TST-001",          // ai_classification の product.code
  "product_name":      "DDR5 ウェハ検査装置",
  "total_value_usd":   4800000,
  "quantity":          2,
  "hs_code":           "9031.49",
  "incoterms":         "CIF",
  "source_module":     "crm",                   // 必ずこの値を指定
  "intended_use":      "S-parameter測定・RF IC特性評価。民間半導体製造専用。"
}
```

**レスポンス（201 Created）:**

```json
{
  "id":            178,
  "case_no":       "CRM-2026-001",
  "status":        "draft",
  "approval_tier": 2,
  "required_steps": ["screening", "ai_run", "catchall"]
}
```

- `id` — AI_TM 内部 transaction_id（以降の参照に使用）
- `approval_tier` — 1: 自動承認 / 2: 標準審査 / 3: 輸出許可確認必要

### 5-2. 取引審査の取得

```
GET https://validation.tsp-aitrademanagement.com/api/transactions/{transaction_id}
GET https://validation.tsp-aitrademanagement.com/api/transactions/recent?limit=20&source_module=crm
```

### 5-3. 判定ステータス一覧

| status 値 | 意味 | CRM 側アクション |
|---|---|---|
| `draft` | 審査準備中 | 待機 |
| `in_review` | AI 審査実行中 | 待機 |
| `pending_approval` | 人間承認待ち | 担当者にエスカレーション通知 |
| `approved` | 輸出可 — APPROVED | 案件を次フェーズへ進める |
| `rejected` | 輸出不可 — REJECTED | 案件停止・法務通知 |
| `needs_review` | 追加情報・輸出許可要 | コンプライアンス部門への確認依頼 |
| `pending_license` | 輸出許可申請中 | 許可証取得待ち |

---

## 6. 制裁スクリーニング API（screening :8005）

CRM 顧客マスタへの登録・更新時、または案件作成時にスクリーニングを実行します。OFAC/BIS/EU/UN/UK — 55,093件のウォッチリストと照合します。

### 6-1. 単一スクリーニング

```
POST https://screening.tsp-aitrademanagement.com/api/screen
Content-Type: application/json

{
  "company_name":   "Huawei Technologies",
  "threshold":      0.75,    // 照合スコア閾値（0.75 推奨）
  "transaction_id": 178      // 任意: 紐付ける transaction_id
}
```

**レスポンス:**

```json
{
  "result_status": "match",
  "max_score":     0.93,
  "matches": [
    {
      "name":       "Huawei Technologies Co., Ltd.",
      "score":      0.93,
      "source":     "BIS_ENTITY_LIST",
      "risk_level": "high",
      "source_id":  "..."
    }
  ]
}
```

`source` の値: `OFAC_SDN` / `BIS_ENTITY_LIST` / `EU_CONSOLIDATED` / `UK_OFSI` / `UN_SC`

### 6-2. バッチスクリーニング（顧客一括取込時）

```
POST https://screening.tsp-aitrademanagement.com/api/screening/batch
Content-Type: application/json

[
  { "company_name": "Samsung Electronics", "threshold": 0.75 },
  { "company_name": "CXMT Co. Ltd.",       "threshold": 0.75 }
]
```

### 6-3. スクリーニング結果と推奨アクション

| result_status | 意味 | 推奨アクション |
|---|---|---|
| `match` | 制裁リストに一致 | CRM で顧客を「リスクフラグ」、担当者即時通知 |
| `possible_match` | 類似一致（要確認） | コンプライアンス担当者へエスカレーション |
| `clear` | 一致なし | 通常処理を継続 |

---

## 7. 品目管理 API（ai_classification :8002）

CRM の商品マスタと AI_TM の品目（ECCN・HS 分類・該非判定）を連動させます。

### 7-1. 品目情報取得

```
GET https://classification.tsp-aitrademanagement.com/api/products/{product_code}
```

**レスポンス（主要フィールド）:**

```json
{
  "id":                    42,
  "code":                  "SEMI-TST-001",
  "name":                  "DDR5 ウェハ検査装置",
  "eccn":                  "3B002",
  "hs_code":               "9031.49",
  "export_control_status": "controlled",
  "export_control_reason": "EAR ECCN 3B002...",
  "usage_summary":         "半導体製造ラインの表面検査工程..."
}
```

`export_control_status` の値: `controlled` / `non_controlled` / `needs_attention`

### 7-2. 品目の新規登録（CRM からの商品連携）

```
POST https://classification.tsp-aitrademanagement.com/products/erp-sync
Content-Type: application/json

{
  "product_code": "CRM-PROD-001",
  "product_name": "新製品名",
  "eccn":         "EAR99",
  "hs_code":      "8543.70",
  "description":  "用途説明",
  "client_id":    "DEMO"
}
```

> **注:** `/products/erp-sync` エンドポイントは ERP との共用です。CRM から呼び出す場合も同じエンドポイントを使用します。`product_code` は CRM の商品コードをそのまま使用できます（upsert — 既存は更新）。

---

## 8. R&D リスク管理 API（rnd_assessment :8003）

CRM の提案・共同研究フェーズに対応する案件情報を保持します。案件の最上流ステージです。

### 8-1. R&D 案件一覧取得

```
GET https://rnd.tsp-aitrademanagement.com/api/cases
```

**レスポンス（各案件の概要）:**

```json
{
  "case_id":     "15370d06-dc37-458c-...",
  "title":       "メモリウェハ検査システム共同研究 2026-Q2（CXMT連携）",
  "status":      "in_progress",
  "description": "...",
  "created_at":  "2026-08-02T..."
}
```

### 8-2. 案件の人物スクリーニング（みなし輸出）

R&D 案件に紐付いた外国籍人材（研究者・共同研究者）のみなし輸出スクリーニング結果を取得できます。CRM の提案フェーズで参照することで、共同研究前のリスクを把握できます。

```
GET https://rnd.tsp-aitrademanagement.com/api/cases/{case_id}/personnel
```

### 8-3. みなし輸出イベントの通知

R&D モジュールは外国籍人材のリスクを検知した際、`POST /api/transactions/events` を通じて ai_validation へ自動通知します。CRM 側でこのイベントをフックする場合は後述の Webhook を使用してください。

---

## 9. 該非判定共有 DB（platform-core /api/hantei）

R&D → 品目管理 → 取引審査の 3 ステージを通じて蓄積された ECCN 該非判定の統合ビューです。CRM は商品・案件に紐付いた判定履歴をここから参照できます。

### 9-1. 判定履歴取得

```
GET https://app.tsp-aitrademanagement.com/api/hantei/records/{product_code}
GET https://app.tsp-aitrademanagement.com/api/hantei/records/{product_code}?source_module=classification
```

**レスポンス（配列）:**

```json
[
  {
    "id":             42,
    "product_code":   "SEMI-TST-001",
    "source_module":  "classification",
    "item_no":        "3B002",
    "item_label":     "ECCN 3B002（半導体テスト装置）",
    "llm_verdict":    "APPLICABLE",
    "llm_confidence": "HIGH",
    "llm_reason":     "S-parameter測定機器は...",
    "decision":       "controlled",
    "recorded_at":    "2026-08-02T..."
  }
]
```

| フィールド | 値 |
|---|---|
| `source_module` | `rnd` / `classification` / `validation` / `crm` |
| `llm_verdict` | `APPLICABLE` / `REVIEW_NEEDED` / `NOT_APPLICABLE` |
| `llm_confidence` | `HIGH` / `MEDIUM` / `LOW` |
| `decision` | `controlled` / `needs_review` / `non_controlled` |

**product_code の形式:**

| 形式 | 元モジュール | 例 |
|---|---|---|
| 品目コード（任意形式） | ai_classification | `SEMI-TST-001` |
| `RND-{case_id}` | rnd_assessment | `RND-15370d06-dc37-...` |
| `linked_product_code` | ai_validation | `SEMI-TST-001`（品目コードと同一） |

### 9-2. 判定レコードの登録（CRM が審査を実施した場合）

```
POST https://app.tsp-aitrademanagement.com/api/hantei/records
Content-Type: application/json

{
  "records": [
    {
      "product_code":  "SEMI-TST-001",
      "source_module": "crm",
      "item_no":       "3B002",
      "item_label":    "ECCN 3B002",
      "llm_verdict":   "APPLICABLE",
      "decision":      "controlled",
      "notes":         "CRM 営業担当者が確認済み"
    }
  ]
}
```

---

## 10. プラットフォーム共通 API（platform-core :8000）

### 10-1. ヘルスチェック

```
GET https://app.tsp-aitrademanagement.com/health

// レスポンス:
{
  "status": "ok",
  "faiss_layers": { "a": true, "b": true, "c": true }
}
```

### 10-2. 各モジュールのヘルス確認

```
GET https://app.tsp-aitrademanagement.com/ui/health/{module_key}
// module_key: ai_validation | ai_classification | screening | rnd_assessment 等
```

### 10-3. 案件（Project）管理

```
GET    https://app.tsp-aitrademanagement.com/api/projects/
POST   https://app.tsp-aitrademanagement.com/api/projects/
GET    https://app.tsp-aitrademanagement.com/api/projects/{project_id}
```

---

## 11. Webhook / イベント設計

### 11-1. AI_TM → CRM（アウトバウンド通知）

ERP と同じ Webhook パターンを CRM にも適用します。

| トリガー | AI_TM 側処理 | CRM 受信エンドポイント（実装必要） |
|---|---|---|
| 取引審査 承認/却下/要確認 | 判定確定後に Bearer 認証付き POST | `POST /crm/webhook/compliance-judgment` |
| スクリーニングフラグ | screening ヒット → ai_validation が flag-for-review | `POST /crm/webhook/screening-alert` |
| みなし輸出リスク検知 | rnd_assessment が DEEMED_EXPORT_RISK イベントを送信 | `POST /crm/webhook/deemed-export-risk` |
| 輸出許可証 期限アラート | platform-core スケジューラーが 90日前/30日前に生成 | （任意）DAP 経由での通知 |

**アウトバウンド Webhook ペイロード（判定通知）:**

```json
{
  "material_code": "SEMI-TST-001",
  "new_judgment":  "APPROVED",
  "new_eccn":      "3B002",
  "rationale":     "...",
  "client_id":     "DEMO"
}
```

`new_judgment` の値: `APPROVED` / `REJECTED` / `PENDING`

### 11-2. CRM → AI_TM（インバウンドイベント）

CRM 側の操作をトリガーに AI_TM にイベントを送る場合に使用します。

```
POST https://validation.tsp-aitrademanagement.com/api/transactions/events
Content-Type: application/json

// 仕向地変更の通知（既存 event_type）
{
  "event_type":    "MATERIAL_ORIGIN_CHANGE",
  "material_code": "SEMI-TST-001",
  "to_country":    "CN"
}

// みなし輸出リスク（既存 event_type）
{
  "event_type":    "DEEMED_EXPORT_RISK",
  "material_code": "SEMI-TST-001",
  "person_name":   "Zhang Wei",
  "nationality":   "CN"
}
```

> **実装必要:** 現在サポートされている `event_type` は `MATERIAL_ORIGIN_CHANGE` と `DEEMED_EXPORT_RISK` のみです。CRM 固有のイベントタイプ（`CRM_DEAL_CREATED` 等）は AI_TM 側への追加実装が必要です。

### 11-3. AI_TM 側 .env に追加が必要な設定

```bash
# CRM Webhook 設定
CRM_WEBHOOK_URL=https://your-crm.example.com/crm/webhook/compliance-judgment
CRM_WEBHOOK_BEARER=your-crm-webhook-secret
MODULE_CRM_URL=http://crm-internal-host:PORT
MODULE_CRM_PUBLIC_URL=https://crm.example.com
```

---

## 12. CRM フィールドマッピング

### 12-1. CRM 案件（Deal / Opportunity）→ ai_validation 取引

| CRM フィールド（一般的） | AI_TM フィールド | 型 | 備考 |
|---|---|---|---|
| Deal ID / Opportunity Number | `case_no` | String(64) | CRM 側で `CRM-{id}` 形式を推奨 |
| Deal Name / 案件名 | `title` | String(255) | 必須 |
| Account / 顧客名 | `counterparty_name` | String(255) | スクリーニングに使用 |
| Ship-To Country / 納入先国 | `destination_country` | ISO alpha-2 | 例: "CN", "US", "TW" |
| End Customer / 最終顧客 | `end_user_name` | String(255) | 最終需要者 |
| End Customer Country | `end_user_country` | ISO alpha-2 | |
| Product Code / SKU | `product_code` | String(64) | ai_classification product.code と一致させる |
| Product Name | `product_name` | String(255) | |
| Deal Value（USD 換算） | `total_value_usd` | Float | De Minimis 計算に使用 |
| Quantity | `quantity` | Float | |
| HS Code | `hs_code` | String(20) | 任意（精度向上） |
| Incoterms | `incoterms` | String(10) | CIF / FOB 等 |
| Use Case / 用途説明 | `intended_use` | Text | AI 判定精度向上に重要 |

### 12-2. CRM 顧客（Account / Contact）→ screening

| CRM フィールド | screening パラメータ | 備考 |
|---|---|---|
| Company Name / 会社名 | `company_name` | スクリーニングの主要キー |
| Country | 参考情報（notes に含める） | 高リスク国フィルタに活用可 |
| CRM Account ID | `transaction_id` に紐付け | 任意: ai_validation 取引に連結 |

---

## 13. データフロー

### 13-1. 標準的な CRM → AI_TM 連携フロー

```
CRM
 │
 ├─ 1. POST /api/screen（顧客名）→ screening:8005
 │    └─ result_status: clear → 次へ / match → エスカレーション
 │
 ├─ 2. POST /api/transactions（案件情報）→ ai_validation:8011
 │    └─ {id, case_no, status: "draft", approval_tier}
 │
 ├─ 3. GET /api/products/{product_code}（ECCN確認）→ ai_classification:8002
 │    └─ {eccn, export_control_status}
 │
 │ （AI_TM 内部で FAISS+Ollama 審査 + スクリーニング連動）
 │
 └─ 4. Webhook 受信: POST /crm/webhook/compliance-judgment ← ai_validation:8011
      └─ {new_judgment: APPROVED/REJECTED/PENDING, new_eccn, rationale}
```

### 13-2. 判定データ継承フロー（plat_hantei_records）

```
rnd_assessment  ──(product_code=RND-{case_id})──┐
ai_classification ─(product_code=product.code)──┤──► plat_hantei_records（PostgreSQL）
ai_validation  ──(product_code=linked_product_code)─┘         │
                                                               │
CRM ──(GET /api/hantei/records/{code})─────────────────────────┘
```

### 13-3. 案件ライフサイクル

```
CRM 提案段階     →  rnd_assessment（R&D リスク評価・みなし輸出確認）
CRM 案件成立     →  ai_classification（ECCN 分類・品目登録）
CRM 受注/見積   →  ai_validation（取引審査・スクリーニング・輸出許可判定）
CRM クローズ     ←  ai_validation Webhook（APPROVED/REJECTED）
```

---

## 14. 認証・環境設定

### 14-1. 接続設定

| モジュール | 内部 URL（サーバー間） | 外部 URL（Cloudflare Tunnel） |
|---|---|---|
| platform-core | `http://localhost:8000` | `https://app.tsp-aitrademanagement.com` |
| ai_validation | `http://localhost:8011` | `https://validation.tsp-aitrademanagement.com` |
| ai_classification | `http://localhost:8002` | `https://classification.tsp-aitrademanagement.com` |
| rnd_assessment | `http://localhost:8003` | `https://rnd.tsp-aitrademanagement.com` |
| screening | `http://localhost:8005` | `https://screening.tsp-aitrademanagement.com` |

### 14-2. CRM 側で必要な環境変数

```bash
AITM_PLATFORM_URL=https://app.tsp-aitrademanagement.com
AITM_VALIDATION_URL=https://validation.tsp-aitrademanagement.com
AITM_SCREENING_URL=https://screening.tsp-aitrademanagement.com
AITM_CLASSIFICATION_URL=https://classification.tsp-aitrademanagement.com
AITM_RND_URL=https://rnd.tsp-aitrademanagement.com
AITM_BEARER=your-aitm-api-key
```

### 14-3. 認証方式（現行と課題）

現在の認証は Bearer トークン方式ですが、一部エンドポイントはトークン不要（開発環境）です。本番 CRM 統合前に以下の対応が必要です:

- JWT 認証またはサービスアカウントキーの厳格化
- エンドポイントごとの認証必須化（現状は開発環境向け設定）
- CRM サービスアカウント用トークンの発行手続き策定

---

## 15. クイックスタート

CRM 開発の最初のステップとして、以下の順に動作確認を行ってください。

**Step 1: ヘルスチェック**

```bash
curl https://app.tsp-aitrademanagement.com/health
curl https://validation.tsp-aitrademanagement.com/health
curl https://screening.tsp-aitrademanagement.com/health
```

**Step 2: スクリーニングのテスト**

```bash
curl -X POST https://screening.tsp-aitrademanagement.com/api/screen \
  -H "Content-Type: application/json" \
  -d '{"company_name": "Samsung Electronics", "threshold": 0.75}'
```

**Step 3: テスト取引の登録**

```bash
curl -X POST https://validation.tsp-aitrademanagement.com/api/transactions \
  -H "Content-Type: application/json" \
  -d '{
    "title": "CRM テスト案件",
    "case_no": "CRM-TEST-001",
    "counterparty_name": "Test Corp",
    "destination_country": "US",
    "source_module": "crm"
  }'
```

**Step 4: 取引審査結果の確認**

```bash
# {id} は Step 3 のレスポンスから取得
curl https://validation.tsp-aitrademanagement.com/api/transactions/{id}
```

**Step 5: 品目の該非判定履歴確認**

```bash
curl https://app.tsp-aitrademanagement.com/api/hantei/records/SEMI-TST-001
```

---

## 16. 実装ロードマップ

| フェーズ | 内容 | AI_TM 側追加実装 | 優先度 |
|---|---|---|---|
| Phase 1（読み込み） | CRM から AI_TM のデータを参照（スクリーニング・取引履歴・品目情報・判定履歴） | なし（既存 API 活用） | 高 |
| Phase 2（案件連携） | CRM の案件成立時に取引審査を自動発火。判定結果を CRM に Webhook 通知 | `CRM_WEBHOOK_URL/BEARER` 対応、`_push_crm_status()` 関数追加 | 高 |
| Phase 3（顧客連動） | CRM 顧客登録・更新時に自動スクリーニング。制裁ヒット時に CRM 顧客へリスクフラグ | `/api/transactions/events` に `CRM_ACCOUNT_CREATED` 追加 | 中 |
| Phase 4（R&D 連携） | CRM の提案段階で rnd_assessment のみなし輸出・共同研究リスクを参照 | rnd_assessment に CRM 向け案件参照 API 追加 | 中 |
| Phase 5（DAP 連携） | DAP（AI オーケストレーター）を CRM チャット UI に組み込み、自然言語でコンプライアンス照会 | DAP に CRM セッション管理の追加 | 低 |

---

## 17. 注意事項・制約

### DB 制約

- **rnd_assessment は SQLite で動作します。** PostgreSQL への移行を試みましたが `DATETIME` 型の非互換があり現在は SQLite 固定です。高負荷アクセスには注意が必要です。
- **ai_validation も SQLite（app.db）で動作します。** 大量の並行書き込みが発生する場合（CRM からの一括案件登録等）は、バッチ登録を推奨します（10件/秒以下）。

### パフォーマンス

- **FAISS インデックスのロードタイム:** platform-core の起動直後（約60秒以内）は FAISS インデックスのプリロード中であるため、`/api/compliance/hantei` の初回呼び出しが遅延します。ヘルスチェックで `faiss_layers.a: true` を確認してから API 呼び出しを行ってください。
- **Ollama（ローカル LLM）の所要時間:** `POST /api/compliance/hantei` は Ollama qwen2.5:7b で最大8件を並列評価するため、10〜30秒かかります。CRM の UI では非同期処理（ポーリングまたは Webhook）で実装してください。

### 重複通知の防止

`erp_case_no` が設定された取引は ERP への Webhook も発火します。CRM と ERP が同一案件を登録する場合は、いずれか一方から登録し、もう一方は参照のみとする設計を推奨します。

### 輸出管理データの機密性

該非判定結果（ECCN・外為法）は輸出管理上の機密情報です。CRM の権限管理で、コンプライアンス部門以外へのアクセスを適切に制限してください。輸出規制関連データの外部漏洩は法的リスクになります。

### 技術的負債（既知）

| 項目 | 状況 | CRM への影響 |
|---|---|---|
| ai_validation / rnd_assessment: SQLite | 既知、未解決 | 高負荷時の書き込み競合に注意 |
| API 認証の一部未整備 | 開発環境向け設定 | 本番前に Bearer トークン必須化が必要 |
| BIS DPL 残 586件未取得 | CSL API offset 上限1000 | BIS 否認リストの一部が未収録 |
| `CRM_WEBHOOK_URL` 未実装 | ERP パターンで追加可能 | Phase 2 で実装必要 |

---

## 参照ファイル

```
CLAUDE.md                                                    — プロジェクト設定・モジュール一覧
modules/ai_validation/app/routers/api_transactions.py        — 取引審査 API 実装
modules/ai_validation/app/services/two_list.py               — 輸出リスト判定ロジック
modules/screening/app/routers/screening.py                   — スクリーニング API 実装
modules/ai_classification/app/routers/products.py            — 品目管理 API 実装
modules/rnd_assessment/app/routers/cases.py                  — R&D 案件 API 実装
platform-core/platform_core/routers/hantei_records.py        — 該非判定共有 DB API
platform-core/platform_core/routers/compliance_assess.py     — FAISS+Ollama 該非判定エンジン
.env                                                         — 環境変数テンプレート
```
