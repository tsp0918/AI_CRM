# CRM（AI_CRM）　3システム連携　開発引き継ぎ書

**文書ID:** HO-CRM-001
**バージョン:** 1.0
**作成日:** 2026-08-14
**宛先:** CRM（AI_CRM）開発チーム
**位置づけ:** 本書1冊で CRM 側の実装に必要な情報が完結します。他文書の参照は不要です。

---

## 目次

1. [背景と全体像](#1-背景と全体像)
2. [業務フロー](#2-業務フロー)
3. [CRM の責務と連携一覧](#3-crm-の責務と連携一覧)
4. [共通仕様](#4-共通仕様)
5. [識別子・キー設計](#5-識別子キー設計)
6. [送信側の実装](#6-送信側の実装)
7. [受信側の実装](#7-受信側の実装)
8. [業務ロジックとゲート制御](#8-業務ロジックとゲート制御)
9. [データモデル変更](#9-データモデル変更)
10. [UI 変更](#10-ui-変更)
11. [環境変数](#11-環境変数)
12. [実装タスクとフェーズ計画](#12-実装タスクとフェーズ計画)
13. [テスト計画](#13-テスト計画)
14. [留意事項](#14-留意事項)
15. [用語集](#15-用語集)

---

# 1. 背景と全体像

## 1.1 何を作るのか

ERP（Mini Global ERP）・CRM（AI_CRM）・AI Trade Management（以下 AI_TM）の3システムを、**引き合いから出荷・売上実績までを1本の商流として貫く**ように連携させます。

CRM は本連携において**業務の起点であり、かつ最終的な実績の集約先**という、最も広い役割を担います。

現状の CRM 側の連携実装は以下の状態です。

| 実装済み | 未実装 |
|---|---|
| 取引先スクリーニング要求（`POST /accounts/{id}/compliance-checks`） | 取引審査の起票（AI_TM への `POST /api/transactions`） |
| 判定結果 Webhook の受け口（`/webhooks/compliance-judgment`） | ERP への契約・受注転記 |
| 制裁リスト更新 Webhook の受け口 | ERP からの実績（出荷・請求）受信 |
| — | **送信元の真正性検証（署名・トークン検証）** |
| — | 見積・契約のドキュメント出力制御 |
| — | エンドユーザーの管理 |

## 1.2 3システムの役割分担

| システム | 役割の核心 | 主管部門 |
|---|---|---|
| **CRM**（本システム） | **顧客・リード・商談・見積・契約の管理（顧客関係の記録）** | 営業・マーケティング |
| **ERP**（Mini Global ERP） | 取引先・品目マスタ、受注・在庫・出荷・請求・会計（取引の記録）<br>**＋ 与信チェック・反社チェック**（本プロジェクトで新設） | 営業事務・物流・経理 |
| **AI_TM** | 輸出コンプライアンス判定・制裁スクリーニング・リスク管理（判定エンジン） | 輸出管理・コンプライアンス |

> **設計原則:** CRM は**判定ロジックを一切持ちません**。AI_TM（輸出リスク）と ERP（商流リスク）から結果を受け取り、それを商談・見積・契約のゲート制御に反映するのが CRM の役割です。

## 1.3 2種類のリスクゲート（重要）

本プロジェクトで、見積の進行を制御するゲートが**2系統**になります。

| ゲート | 判定元 | 対象リスク | 連携 |
|---|---|---|---|
| **輸出ゲート** | AI_TM | 制裁リスト・輸出該非・キャッチオール・De Minimis・みなし輸出 | IF-03 / IF-10 |
| **商流ゲート** | **ERP** | **与信（信用リスク）・反社（反社会的勢力の排除）** | **IF-32**（新規） |

**両方をクリアしないと見積は `DRAFT` から進めません。**

> **ERP のスクリーニング機能の役割変更:** ERP に実装済みのスクリーニング機能は、従来「制裁スクリーニングを AI_TM に委譲する」ものでしたが、本プロジェクトで **「与信チェック・反社チェック」を担う機構へ移行**します。制裁スクリーニングは AI_TM が一元的に担います。
>
> **なお IF-32 は当面ダミー実装です。** ERP 側は hook を受けたら `OK` を返すスタブとして実装され、実際の与信・反社判定ロジックは後日実装されます。CRM 側は**本番同等のインターフェースで呼び出す実装**にしておき、ERP 側の中身が入れ替わってもCRM の改修が不要な状態にしてください。

## 1.4 CRM 側の実装ボリューム（サマリ）

| 分類 | 内容 | 規模 |
|---|---|---|
| 共通基盤 | 送信認証、受信認証、Outbox（再送保証）、Webhook 冪等性 | **新規・大** |
| 2段階審査 | 見積の仮審査フック、契約の正式審査フック、審査キーのハッシュ生成 | **新規・大** |
| ゲート制御 | 輸出ゲート＋商流ゲート、ドキュメント出力制御 | **新規・大** |
| エンドユーザー管理 | 契約相手とは別のエンドユーザー保持・送出・表示 | **新規・中** |
| ERP 連携 | 契約転記、マスタ受信、実績3層の受信 | **新規・大** |
| R&D 起点商談 | AI_TM からの商談作成依頼の受信 | 新規・中 |
| みなし輸出 | 活動ログからのイベント連携 | 新規・中 |
| UI | 商談・見積・契約・取引先・商品マスタ・ダッシュボード | **改修・大** |
| **合計目安** | | **約 149 人日**（§12 の内訳参照） |

---

# 2. 業務フロー

## 2.1 商談作成ルート（4象限）

商談がどのシステムを起点に生まれるかは、顧客と製品の新規／既存の組合せで変わります。

| | 新規顧客 | 既存顧客 |
|---|---|---|
| **新規<br>プロダクト** | **起点: AI_TM**<br>① AI_TM の R&D リスク管理で登録<br>② 品目管理へ移行<br>③ 開発案件に顧客が紐付いた時点で **CRM 側でも商談作成**（← IF-14 で依頼が届く）<br>④ AI_TM で品目が登録され、**ERP 経由で CRM にプロダクトが連動**（← IF-26）された時に商談へ商品を紐付け | **起点: AI_TM**<br>① AI_TM の R&D リスク管理で登録<br>② 同時に CRM では既存顧客で商談作成（← IF-14）<br>③ ERP 経由で CRM にプロダクトが連動（← IF-26）された時に商談へ商品を紐付け |
| **既存<br>プロダクト** | **起点: CRM**<br>① CRM にて新規取引先を登録<br>② 登録と同時に AI_TM へスクリーニングを hook（→ IF-01）<br>③ 品目は登録済みの商品から選択 | **起点: CRM**<br>① CRM 側で商談作成・管理<br>② 取引先・品目とも既存マスタから選択 |

**CRM 側の実装への含意**

- 上段2象限のために、**AI_TM からの商談作成依頼を受け取る Webhook（IF-14）**と、**ERP からの品目マスタ連携を受け取る Webhook（IF-26）**が必要です
- R&D 起点の商談は**専用ステージ（`RND_INCUBATION`）で作成し、パイプライン集計から除外**します（まだ商談化していない開発案件が予実管理の数字を歪めるため）

## 2.2 エンドツーエンドの業務フロー

```
【フェーズ1】取引先スクリーニング
  ERP 登録済の取引先 ─── AI_TM ⇄ ERP で常時スクリーニング、ステータスを保持
  CRM で新規登録された取引先 ─→ 登録段階で AI_TM に hook（IF-01）
                                 └→ 結果が CRM に返る
                                    BLOCK なら CRM 側で取引先にリスクフラグ
                                    商談にもリスクが反映される
  ★ 契約相手だけでなく「エンドユーザー」も同格でスクリーニングする

【フェーズ2】見積 ─ 2つのゲート
  CRM: 見積を DRAFT で作成
    ├→ AI_TM へ取引審査を起票（IF-03, review_type=provisional）… 輸出ゲート
    │    ├ 懸念フラグあり → DRAFT のまま。顧客提出用ドキュメントの出力不可
    │    │                  └→ AI_TM 側で override が承認されれば通過（IF-15）
    │    └ クリア        → 輸出ゲート通過
    └→ ERP へ 与信・反社チェックを依頼（IF-32）… 商流ゲート ★新規
         ├ NG          → DRAFT のまま
         └ OK          → 商流ゲート通過、取引先情報が付与される
  ★ 両ゲート通過で 見積 ISSUABLE → 顧客提出用ドキュメントの出力可

【フェーズ3】契約書発行 ─ 正式審査
  CRM: 契約書発行を hook
    └→ AI_TM へ取引審査を起票（IF-03, review_type=formal, parent_case_no=仮審査）
       ├ 既にリスクがクリアされていれば ★スムーズに完了 → 契約書の出力可
       └ 却下・要確認 → 契約書の出力不可
    └→ AI_TM へライセンス枠の仮引当（IF-07）

【フェーズ4】ERP 連携
  契約 close ＋ 商談 closing
    └→ CRM から ERP へ 取引先・契約情報を連携（IF-25）
       aitm_transaction_id を引き渡し、ERP は新規審査を起票しない

【フェーズ5】出荷（ERP 主導。CRM は結果を受け取る）
  ERP: 出荷伝票を作成 → 契約情報に紐付く
       AI_TM のリスクスクリーニング結果を常に反映、ライセンス残数を消費

【フェーズ6】実績の還流
  ERP: 出荷実績（IF-29）・invoice 発行（IF-30）を CRM へ
    └→ CRM: closing 商談に売上実績を集計。時系列分析が可能になる

【全期間を通じて】継続監視
  AI_TM から CRM へ:
    ├→ 取引先のヒット（IF-11 / IF-12）→ 取引先・商談にフラグ
    └→ 契約期間中のヒット（IF-16）→ 契約にアラート、更新商談の起票をブロック
```

## 2.3 2段階審査の設計意図

| | 仮審査（provisional） | 正式審査（formal） |
|---|---|---|
| 起票トリガー | **CRM で見積を作成** | **CRM で契約書を発行** |
| 目的 | **顧客に提出する前にリスクを止める** | 出荷の法的根拠となる正式判定 |
| ブロック対象 | 見積の `DRAFT` → `ISSUABLE` 遷移、顧客提出用ドキュメントの出力 | 契約書の出力 |
| 解除手段 | AI_TM の override | AI_TM の override（**仮審査の override は継承されない**） |
| 法的位置づけ | 暫定措置。出荷の根拠にはならない | 正式記録。7年保存の監査対象 |

> **なぜ2段階にするのか:** 契約発行時のみの審査では、リスクを含む見積が顧客に提出されてから問題が発覚し、商談の巻き戻しと顧客との信頼毀損が発生します。見積の段階で止められることの実務的価値が大きいためです。

---

# 3. CRM の責務と連携一覧

## 3.1 連携インターフェース カタログ（CRM 関連分）

### 送信（CRM → 他システム）

| ID | 送信先 | 名称 | 種別 | 状況 |
|---|---|---|---|---|
| IF-01 | AI_TM | 取引先スクリーニング（単件） | 同期API | 改修 |
| IF-02 | AI_TM | 取引先スクリーニング（バッチ） | 同期API | 新規 |
| IF-03 | AI_TM | 取引審査の登録（provisional / formal） | 同期API | **新規・中核** |
| IF-04 | AI_TM | 品目の規制情報照会 | 同期API | 新規 |
| IF-05 | AI_TM | 判定履歴の照会 | 同期API | 新規 |
| IF-06 | AI_TM | ライセンス残枠の照会 | 同期API | **新規** |
| IF-07 | AI_TM | ライセンス枠の仮引当／解放 | 同期API | **新規** |
| IF-08 | AI_TM | 審査の取下げ | 同期API | **新規** |
| IF-09 | AI_TM | みなし輸出イベントの連携 | 同期API | **新規** |
| IF-25 | ERP | 契約close時の取引先・契約情報連携 | 同期API | **新規** |
| IF-28 | ERP | 与信枠の照会 | 同期API | **新規** |
| IF-32 | ERP | **与信・反社チェックの依頼** | 同期API | **新規（ERP側は当面ダミー）** |

### 受信（他システム → CRM）

| ID | 送信元 | 名称 | 種別 | 状況 |
|---|---|---|---|---|
| IF-10 | AI_TM | 取引審査 判定結果の通知 | Webhook | 改修 |
| IF-11 | AI_TM | スクリーニングアラート／取引先の名寄せ通知 | Webhook | **新規** |
| IF-12 | AI_TM | 制裁リスト更新による遡及再評価 | Webhook | 改修 |
| IF-13 | AI_TM | みなし輸出リスクの通知 | Webhook | **新規** |
| IF-14 | AI_TM | R&D案件からの商談作成依頼 | Webhook | **新規** |
| IF-15 | AI_TM | override 適用の通知 | Webhook | **新規** |
| IF-16 | AI_TM | 契約期間中の継続監視アラート | Webhook | **新規** |
| IF-26 | ERP | 品目マスタの連携 | Webhook | **新規** |
| IF-27 | ERP | 取引先マスタの連携 | Webhook | **新規** |
| IF-29 | ERP | 出荷実績の連携 | Webhook | **新規** |
| IF-30 | ERP | 請求実績の連携 | Webhook | **新規** |
| IF-31 | ERP | 返品実績の連携 | Webhook | **新規** |

## 3.2 責務分界点

| 領域 | CRM の責務 | 他システムの責務 |
|---|---|---|
| 判定ロジック | **保有しない。**結果を表示・ゲート反映するのみ | AI_TM（輸出）／ERP（与信・反社） |
| **エンドユーザー情報** | **保持・送出・表示** | AI_TM が party 管理・スクリーニング |
| 品目コードのマッピング | **`Product.erp_material_code` の整備を保証** | AI_TM は受け取ったコードで判定 |
| 通貨換算 | **USD 換算値（`total_value_usd`）の算出** | AI_TM は換算しない |
| 審査対象キー | **`review_key_hash` の生成** | AI_TM が再審査要否を判定 |
| ドキュメント出力制御 | **保有**（見積書・契約書のPDF出力ゲート） | — |
| 売上実績の集計 | **保有**（契約額／出荷／請求の3層） | ERP が実績データを供給 |
| 監査記録の正本 | 参照のみ | AI_TM が保有（7年保存） |
| Webhook 受信 | **冪等な受信、10秒以内の応答** | AI_TM / ERP が送信・リトライ |

## 3.3 設計方針

1. **既存の `ScreeningPort` 抽象を踏襲・拡張する。** CRM には既に外部スクリーニングを抽象化する `ScreeningPort`（既定 `MockScreeningAdapter`、本番 `AITMScreeningAdapter`）が存在します。取引審査・与信チェックについても同じパターンで Port を新設し、**モック実装を必ず用意**してください。AI_TM / ERP に接続できない開発環境でも CRM 単体で動作することを維持します。

2. **見積・契約の成立を外部システムの可用性に依存させない。** 審査の起票に失敗しても、見積・契約の作成自体は成立させます。送信は Outbox（送信キュー）経由の非同期とし、失敗時はリトライします。ただし**ゲートは通過させません**（下記4）。

3. **判定不能を「問題なし」として扱わない。** 結果が取得できない場合は `UNKNOWN` として保持し、ゲートは進行をブロックします。**`CLEAR` にフォールバックしてはいけません。** コンプライアンス連携ではフェイルセーフではなく**フェイルクローズ**で設計します。

4. **Webhook 由来の更新は `NEVER_AI_FIELDS` の制約と衝突しない範囲に限定する。** CRM には「人しか書けないフィールド」の機械的強制（`extraction_pipeline.NEVER_AI_FIELDS`）があります。Webhook が更新するのは `ComplianceStatus` と、そこから派生する警告表示・ActionItem のみとし、**ステージ変更・金額確約・VERIFIED 昇格を Webhook から行ってはいけません**。

5. **認証層は連携専用に独立して持つ。** CRM 本体には認証機構がありません（Cookie による自己申告のテナント識別のみ）。本連携の API・Webhook は**それとは独立した Bearer + HMAC の認証層**を持ちます。画面側の認証整備は本連携のスコープ外ですが、**本番投入の必須前提**です。

---

# 4. 共通仕様

## 4.1 認証方式

**全方向で「Bearer トークン ＋ HMAC-SHA256 署名」の二重方式**を採用します。

### 署名アルゴリズム

```
署名対象文字列 = "{timestamp}.{raw_body}"
    timestamp : Unix秒（整数の文字列表現）
    raw_body  : HTTPボディの生バイト列（パース前・再シリアライズ前）

signature = HMAC-SHA256(key=shared_secret, msg=署名対象文字列).hexdigest()
```

### 共通ヘッダ

| ヘッダ | 必須 | 内容 |
|---|---|---|
| `Authorization` | ✔ | `Bearer {token}` |
| `X-Signature` | ✔ | `sha256={hex署名}` |
| `X-Timestamp` | ✔ | Unix秒 |
| `X-Request-Id` | ✔ | UUIDv4。冪等性キーを兼ねる |
| `X-Tenant-Id` | ✔ | テナント識別子（CRM の `crm_tenant_id`） |
| `Content-Type` | ✔ | `application/json; charset=utf-8` |

### シークレット一覧

| 用途 | CRM 側 環境変数 | 相手側 環境変数 |
|---|---|---|
| CRM → AI_TM リクエストの署名 | `AITM_REQUEST_SIGNING_SECRET` | `CRM_INBOUND_SIGNING_SECRET` |
| AI_TM → CRM Webhook の署名 | `AITM_WEBHOOK_SIGNING_SECRET` | `CRM_WEBHOOK_SIGNING_SECRET` |
| CRM → ERP リクエストの署名 | `ERP_REQUEST_SIGNING_SECRET` | `CRM_INBOUND_SIGNING_SECRET`（ERP側） |
| ERP → CRM Webhook の署名 | `ERP_WEBHOOK_SIGNING_SECRET` | `CRM_WEBHOOK_SIGNING_SECRET`（ERP側） |

シークレットは **32バイト以上のランダム値**。リポジトリへのコミット禁止。受信側は**現行鍵と旧鍵の2本を同時に受理**できること（無停止ローテーション。切替期間は最低7日）。

## 4.2 エラーレスポンス規約

```json
{
  "error": {
    "code": "INVALID_SIGNATURE",
    "message": "署名の検証に失敗しました",
    "detail": { "field": "X-Signature" },
    "request_id": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

| HTTP | code | 送信側（CRM）の対応 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | リトライ禁止。ログ記録し運用通知 |
| 401 | `INVALID_SIGNATURE` / `INVALID_TOKEN` / `TIMESTAMP_EXPIRED` | リトライ禁止。即時アラート |
| 403 | `TENANT_FORBIDDEN` | リトライ禁止 |
| 404 | `RESOURCE_NOT_FOUND` | リトライ禁止 |
| 409 | `DUPLICATE_CASE_NO` | **リトライ禁止。**既存 `transaction_id` を採用（正常系） |
| 409 | `LICENSE_QUOTA_CONFLICT` | 再照会のうえ再試行。ユーザーに警告表示 |
| 422 | `UNPROCESSABLE` | リトライ禁止。ActionItem を起票 |
| 429 | `RATE_LIMITED` | `Retry-After` に従いリトライ |
| 5xx | `INTERNAL_ERROR` | **リトライ対象** |

## 4.3 冪等性

| 方向 | 冪等性キー | 挙動 |
|---|---|---|
| 送信（IF-03） | `case_no` ＋ `X-Request-Id` | `409` は正常系。既存 `transaction_id` を採用 |
| 受信（Webhook） | `event_id` | 処理済みは **`200 OK` を返して何もしない**（エラーにしない）。`event_id` は**最低30日保持** |

**Webhook は at-least-once（最低1回）配信で、順序保証がありません。** 全イベントに `occurred_at` と `revision` が含まれるので、**保持中の `revision` より小さいイベントは破棄して `200` を返してください**（古いイベントで新しい状態を上書きしない）。

## 4.4 リトライ規約（Outbox 送信側）

| 項目 | 値 |
|---|---|
| リトライ対象 | タイムアウト、コネクションエラー、`429`、`5xx` |
| リトライ**非**対象 | `400` / `401` / `403` / `404` / `409` / `422` |
| 最大試行回数 | 初回 ＋ 5回 |
| バックオフ | 指数バックオフ ＋ ジッター（±20%）：10秒 → 60秒 → 5分 → 30分 → 2時間 |
| タイムアウト | 接続 5秒 / 読み取り 30秒（同期API）、10秒（Webhook 応答） |
| 全試行失敗時 | DLQ に退避。連携ステータス画面に表示し、手動再送を可能にする |

**受信側（CRM）は 10秒以内に `2xx` を返すこと。** 重い処理（大量の商談への波及など）は非同期化し、受信時点では受理応答のみを返してください。

## 4.5 デグレード動作（重要）

| 状況 | CRM の挙動 |
|---|---|
| IF-01 スクリーニングがタイムアウト | `ComplianceStatus` を `UNKNOWN` として保存。**`CLEAR` として扱わない。**ゲートは進行をブロック |
| IF-03 取引審査の起票が失敗 | 見積・契約の作成自体は成立させる。送信を Outbox に退避しリトライ。画面に「審査未起票」バッジ。**ゲートは通過させない** |
| IF-32 与信・反社チェックが失敗 | 商流ゲートを `UNKNOWN` とし通過させない |
| Webhook が長時間届かない | `PENDING` のまま滞留。**24時間経過で運用アラート** |
| ERP が停止 | 契約 close 時の転記（IF-25）を Outbox に退避しリトライ |

---

# 5. 識別子・キー設計

## 5.1 取引先の2系統

| 系統 | 発生 | ERP `bp_code` | 例 |
|---|---|---|---|
| **ERP由来** | ERP で登録済みの既存取引先 | あり（`BP-XXXXXXX`） | 既存顧客 |
| **CRM発生** | 引き合い時に CRM で新規作成。**ERP には未登録** | **なし** | 新規引き合い先 |

**社名文字列を照合キーにしません。** 以下の `party_ref` 構造体で送出します。

```json
{
  "source_system": "crm",
  "crm_account_id": "8f14e45f-ceea-467a-9f5a-1b2c3d4e5f60",
  "erp_bp_code": null,
  "aitm_party_id": null,
  "legal_name": "Example Semiconductor Co., Ltd.",
  "legal_name_local": "株式会社サンプル半導体",
  "country": "CN",
  "address": "No.1 Example Road, Shanghai",
  "aliases": ["Example Semi"]
}
```

**AI_TM が `aitm_party_id` を採番し、これが3システム共通の取引先識別子になります。** CRM は `Account.aitm_party_id` に保存し、以降のリクエストで送出してください（名寄せをスキップでき、性能と精度が向上します）。

後日その法人が ERP に登録されると、AI_TM 側の名寄せで同一 party にマージされ、**IF-11 の `party.linked` で `erp_bp_code` が通知されます**。CRM はそれを受けて `external_system="erp"` / `external_id=bp_code` を自動設定します。

## 5.2 エンドユーザーの分離管理（重要な新要件）

**輸出管理において、規制判定の核心は「誰が実際に使うか（エンドユーザー）」であり、契約相手ではありません。**

```
【商社経由の取引の例】
  契約相手     : 日本の商社（スクリーニング clear）
  エンドユーザー: 中国の半導体工場（BIS Entity List 掲載）
       ↓
  契約相手だけをスクリーニングすると、リスクを完全に見逃す
```

半導体製造材料・装置という商材の性質上、商社・代理店経由の取引が相当な比率を占めます。

| 項目 | CRM 側の要件 |
|---|---|
| データモデル | 商談・見積・契約に**エンドユーザー情報**を保持（§9.1） |
| 送出 | IF-01 / IF-03 で `counterparty_ref`（契約相手）と **`end_user_ref`** を送る。**同一の場合も明示的に送る** |
| 表示 | 商談・見積・契約の画面に**両方のリスク状態**を表示 |
| ゲート | **より厳しい方**を採用（`match` > `possible_match` > `clear`） |
| 需要者証明書 | エンドユーザーから取得する文書の取得状況を保持し、`end_user_certificate_status` として送出 |

## 5.3 審査の識別子

| 種別 | `case_no` 形式 | 例 |
|---|---|---|
| 仮審査 | `CRM-Q{quote_id}` | `CRM-Q7788` |
| 正式審査 | `CRM-C{contract_id}` | `CRM-C4021` |

- 正式審査は `parent_case_no` に仮審査の `case_no` を設定する
- 契約更新（`relationship_type = renewal`）の場合も新しい `case_no` を採番し、`parent_case_no` に親契約の `case_no` を設定
- `source_module` には常に `"crm"` を設定する

## 5.4 品目コード（前提条件）

CRM は ERP の品目マスタとは別に独自の `Product` マスタを持っています。一方 AI_TM は「ERP の品目コードで判定する」ルールです。

**CRM `Product` に `erp_material_code` を追加し、AI_TM へは常にこの値を送ります。**

| 状況 | 挙動 |
|---|---|
| `erp_material_code` が設定済み | 通常どおり審査を起票 |
| **未設定の品目を含む** | **審査を起票せず、`ComplianceStatus = UNKNOWN`。「品目マッピング未設定」の ActionItem を自動起票。見積・契約の作成自体は成立させる** |

> **運用上の合意事項:** マッピングの整備は本連携の**前提条件**です。開発着手前に、審査対象となりうる CRM `Product` について ERP 品目コードの棚卸しを完了させてください。**これは技術課題ではなく業務準備の課題**であり、開発完了を待つと本番稼働が遅れます。

## 5.5 審査対象キーのハッシュ（再審査判定）

見積は実務上、1つの商談で5〜10版に改訂されます。**見積作成のたびに審査を起票すると、輸出管理担当者の審査待ちキューが実態のない案件で溢れます。**

CRM 側が以下のハッシュを生成して送ります。

```python
def build_review_key_hash(quote_or_contract) -> str:
    items = sorted(
        (li.product.erp_material_code, float(li.quantity))
        for li in quote_or_contract.line_items
    )
    bucket = int(os.environ.get("REVIEW_KEY_VALUE_BUCKET_USD", 100_000))
    parts = [
        json.dumps(items, ensure_ascii=False),
        quote_or_contract.destination_country or "",
        quote_or_contract.end_user_party_id or "",
        (quote_or_contract.end_use or "").strip(),
        str(int(to_usd(quote_or_contract.total_amount) // bucket)),   # 金額帯
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
```

| 状況 | AI_TM の挙動 |
|---|---|
| ハッシュが**同じ** | 再審査せず、既存判定を `200` で返す |
| ハッシュが**変化** | 新リビジョンとして再審査 |
| 契約発行時、仮審査とハッシュ同一かつ有効期限内 | **親ケースの判定を引き継いで即座に承認**（＝スムーズに完了） |

> **金額を段階化する理由:** 値引き交渉で金額が数%動くたびに再審査していては同じ問題が起きます。刻み幅は環境変数で設定可能にしてください（既定 10万USD）。

## 5.6 仮審査結果の有効期限

制裁リストは頻繁に更新されます。**判定結果は判定した瞬間の情報でしかありません。** 見積提出から契約締結まで3か月かかる商談は珍しくありません。

| 項目 | 値 |
|---|---|
| 仮審査の有効期限 | **30日**（AI_TM がレスポンスの `valid_until` で返す） |
| 期限内に契約発行 | 正式審査が即完了 |
| 期限切れで契約発行 | 正式審査でフル再判定が走る |
| **CRM の表示** | **見積画面に「審査鮮度: あと N 日」を表示。期限切れは警告バッジ** |
| 期限切れ時の見積 | `ISSUABLE` → `DRAFT` に自動差し戻す（要判断。既定は差し戻す） |

CRM には既に `GatePolicy` に「コンプライアンス鮮度」の概念があります。この仕組みを流用できます。

## 5.7 通貨

CRM は通貨換算を行わない仕様ですが、AI_TM の De Minimis 計算・閾値判定は **USD 基準**です。

- **CRM 側の責務として `total_value_usd` を必須送信**
- 原通貨の情報も `currency` / `total_value_original` として送り、監査可能にする
- 換算レートは**見積作成日／契約発行日時点**のレートを適用し、`fx_rate` / `fx_rate_date` を送信する

---

# 6. 送信側の実装

## 6.1 共通クライアント（署名生成）

```python
# services/integration_client.py

import hmac, hashlib, json, time, uuid, os, httpx

class SignedClient:
    def __init__(self, base_url: str, tenant_id: str, bearer_env: str, secret_env: str):
        self.base_url = base_url.rstrip("/")
        self.tenant_id = tenant_id
        self._bearer = os.environ[bearer_env]
        self._secret = os.environ[secret_env].encode()

    def post(self, path: str, payload: dict, *, request_id: str | None = None):
        # ★ シリアライズは一度だけ。署名対象と送信ボディは同一バイト列
        body = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        ts = str(int(time.time()))
        sig = hmac.new(self._secret, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        headers = {
            "Authorization": f"Bearer {self._bearer}",
            "X-Signature":   f"sha256={sig}",
            "X-Timestamp":   ts,
            "X-Request-Id":  request_id or str(uuid.uuid4()),
            "X-Tenant-Id":   self.tenant_id,
            "Content-Type":  "application/json; charset=utf-8",
        }
        return httpx.post(f"{self.base_url}{path}", content=body, headers=headers,
                          timeout=httpx.Timeout(connect=5.0, read=30.0))
```

> **重要:** `httpx` の `json=` 引数を使うと内部で再シリアライズされ、署名対象と送信ボディが食い違う可能性があります。**必ず `content=` にバイト列を渡してください。** これは結合テストまで発覚しにくい典型的な不具合です。

## 6.2 Outbox（送信保証）

```python
# services/outbox.py

RETRY_SCHEDULE_SEC = [10, 60, 300, 1800, 7200]

def process_outbox(session, limit: int = 50):
    msgs = (session.query(OutboxMessage)
            .filter(OutboxMessage.status == "pending",
                    or_(OutboxMessage.next_attempt_at.is_(None),
                        OutboxMessage.next_attempt_at <= utcnow()))
            .order_by(OutboxMessage.created_at)
            .limit(limit).with_for_update(skip_locked=True).all())
    for m in msgs:
        result = _dispatch(session, m)
        m.attempt_count += 1
        if result == OutboxResult.SENT:
            m.status, m.sent_at = "sent", utcnow()
        elif result == OutboxResult.FAILED_NO_RETRY:
            m.status = "failed"
        elif m.attempt_count > len(RETRY_SCHEDULE_SEC):
            m.status = "dlq"                       # ★ 運用アラート対象
        else:
            m.next_attempt_at = utcnow() + timedelta(
                seconds=_with_jitter(RETRY_SCHEDULE_SEC[m.attempt_count - 1]))
        session.commit()
```

**実行方式:** 既存の `scripts/daily_snapshot.py` と同様の cron 方式を推奨します（`scripts/process_outbox.py`、**30秒間隔**）。見積作成から審査起票までの遅延を1分以内に収めます。

## 6.3 IF-01　取引先スクリーニング

既存の `ScreeningPort` を拡張します。

```python
class ScreeningPort(Protocol):
    def screen(self, counterparty: PartyRef, end_user: PartyRef | None,
               *, trigger: str) -> ScreeningResult: ...
    def screen_batch(self, refs: list[tuple[PartyRef, PartyRef | None]]) -> BatchAccepted: ...


class AITMScreeningAdapter:
    def screen(self, counterparty, end_user, *, trigger="manual"):
        resp = self._client.post("/api/screen", {
            "counterparty_ref": counterparty.to_dict(),
            "end_user_ref": end_user.to_dict() if end_user else None,
            "threshold": 0.85,
            "enable_watch": True,                     # ★ 常時監視に載せる
            "context": {"trigger": trigger},
        })
        if resp.status_code != 200:
            # ★ 失敗を CLEAR にフォールバックしない
            return ScreeningResult(status=ComplianceStatusValue.UNKNOWN,
                                   error=_parse_error(resp))
        b = resp.json()
        return ScreeningResult(
            status=_map_screening_result(b["overall_result"]),
            counterparty=_parse_party_result(b["counterparty"]),
            end_user=_parse_party_result(b.get("end_user")),
            screening_result_id=b["screening_result_id"],
            detail_url=b.get("detail_url"),
        )


class MockScreeningAdapter:
    """開発環境用。既定は常に CLEAR を返す既存挙動を維持する"""
```

**呼び出し箇所（フックの追加）**

現状のスクリーニング起票は `POST /accounts/{id}/compliance-checks` の明示呼び出しのみです。**引き合い時点で自動的に走るよう、以下のフックを追加**します。

| トリガー | `trigger` 値 | 備考 |
|---|---|---|
| `Account` 新規作成時（画面・API問わず） | `account_created` | **ERP 未登録の取引先を含む。本連携の主要要件** |
| `Lead` → `Engagement` 変換時 | `lead_converted` | 変換で Account が新規作成される場合 |
| **エンドユーザーの登録・変更時** | `end_user_changed` | **新規** |
| 見積作成の直前 | `pre_quote` | 鮮度が古い場合のみ再実行（既定30日超） |
| 手動実行 | `manual` | 既存エンドポイント |

> `Account` 作成のトランザクションをブロックしないよう、**作成コミット後に実行**し、失敗時は Outbox でリトライしてください。

## 6.4 IF-03　取引審査の登録（中核）

### 仮審査（見積作成時）

```python
# services/quote_service.py

def create_quote(session, quote: Quote, actor_id: str) -> Quote:
    quote.status = QuoteStatus.DRAFT
    session.flush()

    # 前提検証：品目マッピング
    unmapped = [li for li in quote.line_items
                if not li.product or not li.product.erp_material_code]
    if unmapped:
        _create_mapping_action_item(session, quote, unmapped)
        _set_gate(session, quote, GateKind.EXPORT, ComplianceStatusValue.UNKNOWN,
                  rationale="ERP品目コード未マッピングのため審査を起票できません")
    else:
        enqueue_outbox(session, kind="aitm.transaction.create",
                       payload=build_transaction_payload(quote, review_type="provisional"),
                       ref_type="quote", ref_id=str(quote.id))
        _set_gate(session, quote, GateKind.EXPORT, ComplianceStatusValue.PENDING)

    # ★ 商流ゲート：ERP へ与信・反社チェックを依頼（IF-32）
    enqueue_outbox(session, kind="erp.credit_compliance_check",
                   payload=build_credit_check_payload(quote),
                   ref_type="quote", ref_id=str(quote.id))
    _set_gate(session, quote, GateKind.COMMERCE, ComplianceStatusValue.PENDING)

    # ライセンス残枠の事前照会（IF-06）
    enqueue_outbox(session, kind="aitm.license.quota_check",
                   payload=build_quota_check_payload(quote),
                   ref_type="quote", ref_id=str(quote.id))
    return quote
```

### 正式審査（契約発行時）

```python
def issue_contract(session, contract: Contract, actor_id: str) -> Contract:
    contract.status = ContractStatus.PENDING_REVIEW
    session.flush()

    parent_quote = contract.source_quote
    enqueue_outbox(session, kind="aitm.transaction.create",
                   payload=build_transaction_payload(
                       contract, review_type="formal",
                       parent_case_no=f"CRM-Q{parent_quote.id}" if parent_quote else None),
                   ref_type="contract", ref_id=str(contract.id))
    _set_gate(session, contract, GateKind.EXPORT, ComplianceStatusValue.PENDING)

    # ライセンス枠の仮引当（IF-07）
    enqueue_outbox(session, kind="aitm.license.allocate",
                   payload=build_allocation_payload(contract),
                   ref_type="contract", ref_id=str(contract.id))
    return contract
```

### ペイロード生成

```python
def build_transaction_payload(doc, *, review_type: str,
                              parent_case_no: str | None = None) -> dict:
    eng = doc.engagement
    prefix = "Q" if review_type == "provisional" else "C"
    return {
        "case_no": f"CRM-{prefix}{doc.id}",
        "source_module": "crm",
        "review_type": review_type,
        "parent_case_no": parent_case_no,
        "review_key_hash": build_review_key_hash(doc),

        "counterparty_ref": build_party_ref(eng.account),
        "end_user_ref":     build_party_ref(doc.end_user_account or eng.account),
        "end_user_certificate_status": doc.end_user_certificate_status or "not_obtained",

        "destination_country": doc.destination_country or (
            doc.end_user_account or eng.account).country,
        "end_use": doc.end_use,

        "items": [
            {
                "line_no": i + 1,
                "product_code": li.product.erp_material_code,   # ★ ERP品目コード
                "crm_product_id": str(li.product_id),
                "product_name": li.product_name_snapshot,
                "quantity": float(li.quantity),
                "unit": li.unit,
                "unit_price_usd": to_usd(li.unit_price, doc.currency, doc.fx_rate),
                "amount_usd":     to_usd(li.amount,     doc.currency, doc.fx_rate),
            }
            for i, li in enumerate(doc.line_items)
        ],

        "total_value_usd":      to_usd(doc.total_amount, doc.currency, doc.fx_rate),
        "currency":             doc.currency,
        "total_value_original": float(doc.total_amount),
        "fx_rate":              float(doc.fx_rate),
        "fx_rate_date":         doc.fx_rate_date.isoformat(),

        "contract_start_date":  doc.start_date.isoformat() if doc.start_date else None,
        "contract_end_date":    doc.end_date.isoformat() if doc.end_date else None,
        "requested_delivery_date": (doc.requested_delivery_date.isoformat()
                                    if doc.requested_delivery_date else None),
        "incoterms": doc.incoterms,

        "crm_context": {
            "quote_id":    doc.id if review_type == "provisional" else None,
            "contract_id": doc.id if review_type == "formal" else None,
            "engagement_id": eng.id,
            "relationship_type": eng.relationship_type or "new",
            "owner_user_name": eng.owner_user.name if eng.owner_user else None,
        },
    }


def build_party_ref(account: Account) -> dict:
    return {
        "source_system": "erp" if account.external_system == "erp" else "crm",
        "crm_account_id": str(account.id),
        "erp_bp_code": account.external_id if account.external_system == "erp" else None,
        "aitm_party_id": account.aitm_party_id,   # 既知なら送る（名寄せをスキップできる）
        "legal_name": account.legal_name_en or account.name,
        "legal_name_local": account.name,
        "country": account.country,
        "address": account.address,
        "aliases": account.aliases or [],
    }
```

### レスポンス処理

```python
def handle_transaction_response(session, doc, resp: httpx.Response):
    if resp.status_code in (200, 201):
        body = resp.json()
    elif resp.status_code == 409:
        # ★ エラーではない。ネットワーク再送時の正常系
        body = resp.json()["error"]["detail"]
    elif resp.status_code == 422:
        _create_action_item_from_422(session, doc, resp.json())
        _set_gate(session, doc, GateKind.EXPORT, ComplianceStatusValue.UNKNOWN)
        return OutboxResult.FAILED_NO_RETRY
    elif resp.status_code in (400, 401, 403, 404):
        _alert_ops(resp)
        return OutboxResult.FAILED_NO_RETRY
    else:                                   # 429 / 5xx / タイムアウト
        return OutboxResult.RETRY

    doc.aitm_transaction_id = body["transaction_id"]
    doc.aitm_case_no        = body["case_no"]
    doc.aitm_status         = body["status"]
    doc.aitm_review_type    = body["review_type"]
    doc.aitm_valid_until    = parse_iso(body["valid_until"]) if body.get("valid_until") else None
    doc.aitm_revision       = body.get("revision", 1)
    doc.aitm_submitted_at   = utcnow()
    if body.get("counterparty_party_id"):
        doc.engagement.account.aitm_party_id = body["counterparty_party_id"]
    if body.get("end_user_party_id") and doc.end_user_account:
        doc.end_user_account.aitm_party_id = body["end_user_party_id"]
    return OutboxResult.SENT
```

## 6.5 IF-06 / IF-07　ライセンス枠の照会と仮引当

**見積段階で残枠を照会することが本機能の要点です。** 出荷段階で枠不足が発覚すると、契約済みなのに出荷できず、新規許可申請に数週間〜数か月かかります。

```python
class AITMLicensePort:
    def check_quota(self, quote) -> QuotaCheckResult:
        """IF-06 見積作成時に呼ぶ。残枠不足なら見積画面に警告を表示"""
        resp = self._client.post("/api/licenses/quota-check", {
            "items": [{"product_code": li.product.erp_material_code,
                       "quantity": float(li.quantity), "unit": li.unit,
                       "amount_usd": to_usd(li.amount, quote.currency, quote.fx_rate)}
                      for li in quote.line_items],
            "destination_country": quote.destination_country,
            "end_user_party_id": quote.end_user_account.aitm_party_id if quote.end_user_account else None,
            "contract_start_date": ..., "contract_end_date": ...,
            "context": {"case_no": f"CRM-Q{quote.id}", "purpose": "quote"},
        })
        ...

    def allocate(self, contract) -> AllocationResult:
        """IF-07 契約発行時に枠を仮引当"""

    def release(self, allocation_id: str):
        """IF-07 契約キャンセル・失注時に解放"""
```

**警告の表示例（見積画面）**

```
⚠ 許可証 J-2026-00412 の残枠が 20 L 不足します。
   新規申請の想定リードタイムは 8 週間です。納期の調整をご検討ください。
⚠ 許可証の有効期限（2027-03-31）が契約終了日（2027-08-31）より前に到来します。
```

## 6.6 IF-08　審査の取下げ

商談が失注した場合、審査案件が開いたまま残り、**輸出管理担当者の審査待ちキューが実態のない案件で滞留します。**

| CRM 側のイベント | `reason_code` | 付随処理 |
|---|---|---|
| 商談が Closed Lost | `opportunity_lost` | 配下の全審査（仮・正式）を取下げ |
| 見積が失効・破棄 | `quote_discarded` | 仮審査を取下げ |
| 契約がキャンセル | `contract_cancelled` | 正式審査を取下げ ＋ **ライセンス仮引当を解放（IF-07）** |
| 条件変更で新ケースに置換 | `superseded` | 旧ケースを取下げ |

## 6.7 IF-09　みなし輸出イベントの連携

外為法上、**外国籍の人物・外国法人への機密技術の提供・開示は「輸出」とみなされます**。つまり、モノが動く前の営業・開発活動そのものが規制対象です。実際に外国籍顧客と技術情報をやり取りするのは営業・技術営業であり、**CRM の活動ログが判定の入力になるべき**です。

| CRM 側の記録 | `event_type` |
|---|---|
| 技術資料・仕様書の共有 | `technical_document_shared` |
| サンプル提供 | `sample_provided` |
| 技術打合せ（相手方参加者を含む） | `technical_meeting` |
| 工場見学・ラボツアー | `facility_tour` |

**CRM 側で必要な対応**

- 活動ログ（`IngestionSource` / `Touch`）に「技術情報の授受を含む」フラグと `event_type` を追加
- **相手方参加者の国籍・所属を記録できるようにする**（現状 `Contact` に国籍フィールドがない）
- 対象顧客が外国法人・外国籍の場合、記録時に注意喚起を表示

> **特に注意が必要な相手先:** 大学・研究機関との共同開発、外国籍研究者が在籍する顧客、在日外国法人の子会社。

## 6.8 IF-32　ERP への与信・反社チェック依頼（新規）

**ERP に実装済みのスクリーニング機能が、「制裁スクリーニング（AI_TM 委譲）」から「与信チェック・反社チェック」を担う機構へ移行します。** 見積を DRAFT で作成したタイミングで、CRM から ERP のこのスクリーニングへ流します。

> **ERP 側は当面ダミー実装です。** hook を受けたら `OK` を返すスタブとして実装されます。**CRM 側は本番同等のインターフェースで呼び出す実装にしておき、ERP 側の中身が入れ替わっても CRM の改修が不要な状態**にしてください。

```
POST {ERP_BASE_URL}/gts/screening/commerce-check
```

**リクエスト**

```json
{
  "request_type": "quote_draft",
  "crm_quote_id": 7788,
  "crm_engagement_id": 3310,
  "counterparty": {
    "crm_account_id": "8f14e45f-...",
    "erp_bp_code": "BP-1000001",
    "legal_name": "Example Trading Co., Ltd.",
    "legal_name_local": "サンプル商事株式会社",
    "country": "JP",
    "address": "1-1 Marunouchi, Tokyo",
    "corporate_number": "1234567890123",
    "representative_name": "田中 一郎"
  },
  "end_user": {
    "crm_account_id": "b2c3d4e5-...",
    "erp_bp_code": null,
    "legal_name": "Example Semiconductor Co., Ltd.",
    "country": "CN"
  },
  "check_types": ["credit", "antisocial"],
  "amount": { "currency": "JPY", "total_amount": 12750000,
              "total_value_usd": 85000.00 },
  "payment_terms": "NET60",
  "requested_delivery_date": "2026-09-30"
}
```

**レスポンス `200`（ダミー実装時も同じ形）**

```json
{
  "check_id": "CMC-000512",
  "overall_result": "ok",
  "checked_at": "2026-08-14T05:15:00.000Z",
  "revision": 1,
  "results": {
    "credit": {
      "result": "ok",
      "credit_limit": 50000000,
      "credit_used": 12000000,
      "credit_available": 38000000,
      "exceeds_limit": false,
      "rating": "B+",
      "rationale": null
    },
    "antisocial": {
      "result": "ok",
      "matched_entries": [],
      "checked_databases": ["社内DB"],
      "rationale": null
    }
  },
  "counterparty_attributes": {
    "erp_bp_code": "BP-1000001",
    "payment_terms_master": "NET60",
    "customer_group": "DOMESTIC_TRADING",
    "sales_district": "JP-KANTO"
  },
  "detail_url": "https://erp.example.com/ui/gts/commerce-checks/CMC-000512"
}
```

| `overall_result` | CRM の商流ゲート | 挙動 |
|---|---|---|
| `ok` | PASS | 通過 |
| `warning` | WARN | 警告表示のうえ通過可。承認 ActionItem を起票 |
| `ng` | **BLOCK** | 見積を `DRAFT` に固定 |
| `pending` | PENDING | 審査中バッジ。通過させない |

**`counterparty_attributes` の扱い（「取引先情報を付与する」の実装）**

レスポンスに含まれる ERP 側の取引先属性を、CRM の `Account` に反映します。これにより、**ERP を正とする取引先情報（与信枠・支払条件・顧客グループ等）が見積作成のタイミングで CRM に取り込まれます。**

| ERP からの属性 | CRM の反映先 |
|---|---|
| `erp_bp_code` | `Account.external_system="erp"` / `external_id` |
| `credit_limit` / `credit_available` | `Account.credit_limit` / `credit_available`（参照専用） |
| `payment_terms_master` | 見積の既定支払条件 |
| `customer_group` / `sales_district` | `Account` の分類属性 |

> **CRM 発生（ERP 未登録）の取引先の場合:** `erp_bp_code` が `null` で送られます。ERP 側は「ERP 未登録の取引先」として扱い、与信枠は `null`、`overall_result` は `warning`（与信枠未設定）を返す想定です。ダミー実装期間中は `ok` が返ります。

## 6.9 IF-25　ERP への契約・受注転記

契約 close ＋ 商談 closing のタイミングで、取引先・契約情報を ERP へ連携します。

**最重要:** `aitm_transaction_id` を必ず引き渡してください。これがないと **ERP が新規に取引審査を起票し、審査案件が二重生成されます。** 結果として出荷伝票が CRM 発の審査とは別の案件に紐づき、「出荷実績を CRM の契約商談で確認する」という連鎖が切れます。

```json
POST {ERP_BASE_URL}/sd/sales-orders
{
  "crm_contract_id": 4021,
  "crm_engagement_id": 3310,
  "aitm_transaction_id": "TXN-000999",      ← ★ 必須
  "skip_export_check": true,                 ← ★ ERP側で新規審査を起票させない
  "customer_code": "BP-1000001",
  "counterparty": { ... },                   ← ERP未登録の場合はBP新規作成用の情報
  "end_user": { ... },                       ← ★ エンドユーザー情報
  "customer_po_number": "CRM-C4021",
  "document_date": "2026-08-20",
  "requested_delivery_date": "2026-09-30",
  "contract_start_date": "2026-09-01",
  "contract_end_date": "2027-08-31",
  "incoterms": "CIF",
  "payment_terms": "NET60",
  "currency": "JPY",
  "items": [
    { "material_code": "MAT-1000001", "quantity": 100, "unit": "L",
      "unit_price": 850.00 }
  ]
}
```

レスポンスの `document_number`（受注番号）を `Contract.erp_sales_order_number` に保存します。

---

# 7. 受信側の実装

## 7.1 受信認証

```python
# api/webhook_security.py

CLOCK_SKEW_TOLERANCE_SEC = 300

async def verify_webhook(request: Request, source: str) -> WebhookContext:
    raw = await request.body()                      # ★ 生バイト列
    ts  = request.headers.get("X-Timestamp", "")
    sig = request.headers.get("X-Signature", "")

    if not ts.isdigit() or abs(time.time() - int(ts)) > CLOCK_SKEW_TOLERANCE_SEC:
        raise HTTPException(401, _err("TIMESTAMP_EXPIRED", "タイムスタンプの有効期限切れ"))

    secret_env = {"aitm": "AITM_WEBHOOK_SIGNING_SECRET",
                  "erp":  "ERP_WEBHOOK_SIGNING_SECRET"}[source]
    for secret in _active_secrets(secret_env):      # 現行鍵＋旧鍵
        expected = "sha256=" + hmac.new(secret, f"{ts}.".encode() + raw,
                                        hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, sig):      # ★ 定数時間比較
            break
    else:
        raise HTTPException(401, _err("INVALID_SIGNATURE", "署名の検証に失敗しました"))

    if not _verify_bearer(request.headers.get("Authorization"), source):
        raise HTTPException(401, _err("INVALID_TOKEN", "トークンが不正です"))

    tenant_id = request.headers.get("X-Tenant-Id")
    if not _tenant_exists(tenant_id):
        raise HTTPException(403, _err("TENANT_FORBIDDEN", "無効なテナントです"))

    return WebhookContext(tenant_id=tenant_id, raw=raw,
                          payload=json.loads(raw.decode("utf-8")))
```

> **既存 `webhooks.py` の改修点:** 現状は `X-Tenant-Id` ヘッダのみで宛先を決めており、送信元の真正性を検証していません（既知の課題として文書化済み）。本実装で解消します。

## 7.2 共通の受信ハンドラ骨格

```python
async def _handle(request: Request, source: str, processor):
    ctx = await verify_webhook(request, source)
    ev  = ctx.payload
    event_id = ev["event_id"]

    with tenant_session(ctx.tenant_id) as session:
        # ① 冪等性 — 重複はエラーにせず 200 を返す
        if session.get(WebhookEvent, event_id):
            return {"status": "duplicate"}

        session.add(WebhookEvent(event_id=event_id, tenant_id=ctx.tenant_id,
                                 event_type=ev["event_type"], payload=ev,
                                 received_at=utcnow(), result="processed"))
        try:
            # ② 順序解決 — 古い revision は破棄
            outcome = processor(session, ev["data"], ev["occurred_at"])
            session.query(WebhookEvent).filter_by(event_id=event_id).update(
                {"result": outcome, "processed_at": utcnow()})
            session.commit()
        except Exception as e:
            session.rollback()
            _record_error(ctx.tenant_id, event_id, e)
            raise HTTPException(500, _err("INTERNAL_ERROR", "処理に失敗しました"))
    return {"status": outcome}
```

> **Webhook のレスポンスに業務データを載せないこと。** 既存の `sanctions-list-updated` は影響 Engagement 一覧を返す実装ですが、**受理応答のみを返す設計に変更**します。リトライ時に副作用が読めなくなるためです。AI_TM チームと併せて調整が必要です。

## 7.3 受信エンドポイント一覧

| ID | パス | 送信元 | 処理内容 |
|---|---|---|---|
| IF-10 | `POST /webhooks/compliance-judgment` | AI_TM | 見積・契約の審査ステータス更新、ゲート再評価、ActionItem 起票 |
| IF-11 | `POST /webhooks/screening-alert` | AI_TM | 取引先の `ComplianceStatus` 更新、進行中商談への警告。`party.linked` は `external_id` の自動設定 |
| IF-12 | `POST /webhooks/sanctions-list-updated` | AI_TM | 複数取引先・契約の一括更新 |
| IF-13 | `POST /webhooks/deemed-export-risk` | AI_TM | 取引先に注意フラグ、輸出管理部門への確認 ActionItem |
| IF-14 | `POST /webhooks/rnd-opportunity` | AI_TM | **R&D 案件からの商談自動作成** |
| IF-15 | `POST /webhooks/override-applied` | AI_TM | override の事実と理由を保存・表示、ゲート解除 |
| IF-16 | `POST /webhooks/contract-monitoring` | AI_TM | 契約へのアラート、更新商談の起票ブロック |
| IF-26 | `POST /webhooks/erp/material-updated` | ERP | 品目マスタの同期（`ErpMaterial` 更新） |
| IF-27 | `POST /webhooks/erp/business-partner-updated` | ERP | 取引先マスタの同期 |
| IF-29 | `POST /webhooks/erp/delivery-posted` | ERP | **出荷実績の計上** |
| IF-30 | `POST /webhooks/erp/billing-posted` | ERP | **請求実績の計上** |
| IF-31 | `POST /webhooks/erp/return-posted` | ERP | 返品実績（マイナス計上） |

## 7.4 IF-10　判定結果の処理

```python
def process_judgment(session, data: dict, occurred_at: str) -> str:
    doc = _resolve_document(session, data)   # quote_id / contract_id / transaction_id で解決
    if doc is None:
        return "error"                       # 対象不明。記録して 200 を返す

    # 順序解決：古いイベントは破棄
    if doc.aitm_revision and data["revision"] <= doc.aitm_revision:
        return "stale"

    doc.aitm_status      = data["status"]
    doc.aitm_revision    = data["revision"]
    doc.aitm_judged_at   = parse_iso(data["judged_at"])
    doc.aitm_valid_until = parse_iso(data["valid_until"]) if data.get("valid_until") else None

    crm_status = MAP_JUDGMENT_TO_CRM[data["status"]]
    _set_gate(session, doc, GateKind.EXPORT, crm_status,
              rationale=data["judgment"]["rationale"],
              detail_url=data["detail_url"],
              red_flags=data["judgment"].get("red_flags"))

    if crm_status in (ComplianceStatusValue.NEEDS_REVIEW,
                      ComplianceStatusValue.PENDING_LICENSE,
                      ComplianceStatusValue.HIT):
        create_action_item(
            session, engagement_id=doc.engagement_id, field_path="compliance",
            title=f"輸出管理部門への確認（{data['status']}）",
            description=data["judgment"]["rationale"],
            due_date=utcnow().date() + timedelta(days=3),
            created_by="aitm-webhook")

    reevaluate_gates(session, doc)
    return "processed"


MAP_JUDGMENT_TO_CRM = {
    "draft":            ComplianceStatusValue.PENDING,
    "in_review":        ComplianceStatusValue.PENDING,
    "pending_approval": ComplianceStatusValue.PENDING,
    "approved":         ComplianceStatusValue.CLEAR,
    "needs_review":     ComplianceStatusValue.NEEDS_REVIEW,
    "pending_license":  ComplianceStatusValue.PENDING_LICENSE,
    "rejected":         ComplianceStatusValue.HIT,
    "withdrawn":        ComplianceStatusValue.WITHDRAWN,
}
```

## 7.5 IF-14　R&D 案件からの商談自動作成

```python
def process_rnd_opportunity(session, data: dict, occurred_at: str) -> str:
    cust = data["customer"]

    if cust["is_existing"] and cust.get("crm_account_id"):
        account = session.get(Account, cust["crm_account_id"])
    else:
        account = Account(name=cust["legal_name"], legal_name_en=cust["legal_name"],
                          country=cust["country"], address=cust.get("address"),
                          aitm_party_id=cust.get("aitm_party_id"))
        session.add(account)
        session.flush()
        # 新規取引先は即スクリーニング
        enqueue_outbox(session, kind="aitm.screen",
                       payload=build_screen_payload(account, trigger="rnd_opportunity"),
                       ref_type="account", ref_id=str(account.id))

    eng = Engagement(
        account_id=account.id,
        name=data["rnd_case_title"],
        stage=EngagementStage.RND_INCUBATION,   # ★ 専用ステージ
        relationship_type="rnd_origin",
        aitm_rnd_case_id=data["rnd_case_id"],
        exclude_from_pipeline=True,             # ★ 予実集計から除外
    )
    session.add(eng)
    session.flush()

    # AI_TM へ商談IDを返す（Webhookのレスポンスではなく別APIで）
    enqueue_outbox(session, kind="aitm.rnd.link_opportunity",
                   payload={"rnd_case_id": data["rnd_case_id"],
                            "crm_engagement_id": eng.id,
                            "crm_account_id": str(account.id)},
                   ref_type="engagement", ref_id=str(eng.id))
    return "processed"
```

> **運用上の注意:** 自動作成すると「まだ商談化していない開発案件」が CRM のパイプラインに混入し、予実管理の数字が歪みます。**専用ステージ `RND_INCUBATION` に隔離し、`exclude_from_pipeline=True` でパイプライン集計から除外**してください。商談化の承認を経て通常ステージへ移行させます。

## 7.6 IF-29 / IF-30 / IF-31　実績の受信（3層管理）

「invoice 発行を hook に売上実績を集計」だけでは実務の数字が合いません。

```
契約: 1000個 / 1億円
  ├ 第1回出荷: 300個 → invoice 3000万円
  ├ 第2回出荷: 400個 → invoice 4000万円
  └ 第3回出荷: 未実施
```

7000万円だけが渡ると、**残り3000万が受注残なのか失注なのかが分かりません。**

| 層 | 定義 | 連携元 |
|---|---|---|
| **契約額** | 契約時点の総額 | CRM が自身で保持 |
| **出荷実績** | 実際に出荷された数量・金額の累計 | ERP の Delivery（IF-29） |
| **請求実績** | invoice 発行済みの金額累計 | ERP の BillingDocument（IF-30） |

| 指標 | 算式 |
|---|---|
| 受注残 | 契約額 − 出荷実績 |
| 未請求残 | 出荷実績 − 請求実績 |
| 消化率 | 出荷実績 ÷ 契約額 |

```python
def process_delivery_posted(session, data: dict, occurred_at: str) -> str:
    contract = _find_contract_by_erp_so(session, data["erp_sales_order_number"])
    if contract is None:
        return "error"
    if _already_applied(session, contract, "delivery", data["erp_delivery_number"]):
        return "duplicate"

    for item in data["items"]:
        session.add(ContractFulfillment(
            contract_id=contract.id, kind="shipment",
            erp_document_number=data["erp_delivery_number"],
            product_code=item["material_code"],
            quantity=item["quantity"],
            amount=item["amount"], currency=data["currency"],
            posted_at=parse_iso(data["posted_at"])))
    _recalculate_actuals(session, contract)   # 3層の再集計
    return "processed"
```

**返品（IF-31）はマイナス計上**として同じテーブルに `kind="return"` で記録します。

---

# 8. 業務ロジックとゲート制御

## 8.1 2系統のゲート

```python
class GateKind(str, Enum):
    EXPORT   = "export"     # AI_TM 由来（制裁・輸出該非・みなし輸出）
    COMMERCE = "commerce"   # ERP 由来（与信・反社）
```

| `ComplianceStatus.status` | ゲート判定 | 挙動 |
|---|---|---|
| `CLEAR` | PASS | 通過可 |
| `PENDING` | **BLOCK** | 「審査中」バッジ。**見積は DRAFT のまま** |
| `FLAGGED` | WARN | 警告表示のうえ通過可。承認 ActionItem を要求 |
| `NEEDS_REVIEW` | **BLOCK** | 輸出管理部門への確認 ActionItem を要求 |
| `PENDING_LICENSE` | WARN | 「出荷不可・許可証待ち」を明示。見積・契約は進められる |
| `OVERRIDDEN` | PASS | **override により通過。**理由を必ず表示 |
| `HIT` | **BLOCK** | 進行を禁止。`Waiver` でのみ通過可（要輸出管理部門承認） |
| `UNKNOWN` | **BLOCK** | **判定不能はブロック（フェイルクローズ）** |
| `WITHDRAWN` | — | 取下げ済み。再起票が必要 |

**見積の遷移条件**

```python
def can_issue_quote(quote) -> tuple[bool, list[str]]:
    reasons = []
    if gate_status(quote, GateKind.EXPORT) not in PASSING:
        reasons.append("輸出コンプライアンス審査が未完了です")
    if gate_status(quote, GateKind.COMMERCE) not in PASSING:
        reasons.append("与信・反社チェックが未完了です")
    if account_status(quote.engagement.account) == ComplianceStatusValue.HIT:
        reasons.append("取引先が制裁対象です")
    if quote.end_user_account and account_status(quote.end_user_account) == ComplianceStatusValue.HIT:
        reasons.append("エンドユーザーが制裁対象です")
    return (not reasons), reasons

PASSING = {ComplianceStatusValue.CLEAR,
           ComplianceStatusValue.OVERRIDDEN,
           ComplianceStatusValue.FLAGGED,          # 警告付きで通過
           ComplianceStatusValue.PENDING_LICENSE}
```

## 8.2 ドキュメント出力制御

| 審査状態 | 社内検討用<br>（DRAFT透かし） | 顧客提出用<br>見積書 | 技術仕様書 | 契約書 |
|---|---|---|---|---|
| 取引先／エンドユーザーが BLOCK | ✕ | ✕ | ✕ | ✕ |
| 仮審査 未実施／審査中 | ○ | ✕ | ✕ | ✕ |
| 仮審査 懸念あり（未 override） | ○ | ✕ | ✕ | ✕ |
| **商流ゲート NG（与信・反社）** | ○ | ✕ | ✕ | ✕ |
| 仮審査 クリア／override 済 ＋ 商流ゲート OK | ○ | ○ | ○ | ✕ |
| 正式審査 審査中 | ○ | ○ | ○ | ✕ |
| 正式審査 クリア | ○ | ○ | ○ | ○ |
| 正式審査 却下 | ○ | ✕ | ✕ | ✕ |

> **DRAFT 透かしの活用:** 未クリア時に一切出力できないと、社内検討や上長への相談すらできず実務が止まります。**「社内検討用（DRAFT透かし入り・社外持出禁止の明記）」だけは出力可**とします。

```python
def can_export_document(doc, doc_kind: DocumentKind) -> tuple[bool, str | None]:
    matrix = DOCUMENT_EXPORT_MATRIX[doc_kind]
    state  = resolve_review_state(doc)
    if matrix.get(state, False):
        return True, None
    return False, EXPORT_BLOCK_REASONS[state]
```

出力可否は**サーバ側で強制**してください。画面でボタンを隠すだけでは不十分です（URL 直打ちで出力できてしまいます）。

## 8.3 商談・見積・契約の作成ブロック

| 対象の状態 | ブロックする操作 |
|---|---|
| 取引先が `HIT` | 新規商談・見積・契約の作成。既存商談の閲覧・記録は許可（監査のため） |
| **エンドユーザーが `HIT`** | 同上 |
| 契約が継続監視でヒット（IF-16） | **更新商談（renewal）の起票** |

## 8.4 override の表示

override は AI_TM 側で承認されます。CRM は結果を受け取って表示するだけですが、**表示は必須**です。

営業が「なぜこの案件は通ったのか」を理解していないと、**顧客に誤った説明をするリスク**があります（「うちは審査を通っています」と言ってしまう等）。

| 表示項目 | 内容 |
|---|---|
| バッジ | 「override により通過」 |
| 承認者 | 氏名・役職 |
| 理由 | 全文 |
| 有効期限 | 「あと N 日」。期限切れ間近は警告色 |
| 適用範囲 | この見積のみ／この契約のみ／取引先全体 |
| 詳細リンク | AI_TM の該当画面へ |

---

# 9. データモデル変更

## 9.1 既存モデルへの追加

```python
# models/party.py
class Account(Base):
    ...
    aitm_party_id       = Column(String(32), nullable=True, index=True)
    legal_name_en       = Column(String(255), nullable=True)
    aliases             = Column(JSONB, nullable=True)
    # ERP から付与される属性（IF-32 / IF-27 で更新）
    credit_limit        = Column(Numeric(18, 2), nullable=True)
    credit_available    = Column(Numeric(18, 2), nullable=True)
    customer_group      = Column(String(64), nullable=True)
    sales_district      = Column(String(64), nullable=True)
    payment_terms       = Column(String(32), nullable=True)

class Contact(Base):
    ...
    nationality         = Column(String(2), nullable=True)   # ★ みなし輸出判定用
    affiliation         = Column(String(255), nullable=True)


# models/product.py
class Product(Base):
    ...
    erp_material_code   = Column(String(32), nullable=True, index=True)
    # 参照用にキャッシュする規制情報（IF-04 / IF-26 で更新）
    eccn                = Column(String(32), nullable=True)
    hs_code             = Column(String(32), nullable=True)
    fefta_judgment      = Column(String(32), nullable=True)
    country_of_origin   = Column(String(2), nullable=True)
    regulation_synced_at= Column(DateTime(timezone=True), nullable=True)


# models/engagement.py
class Engagement(Base):
    ...
    aitm_rnd_case_id     = Column(String(32), nullable=True, index=True)
    exclude_from_pipeline= Column(Boolean, nullable=False, default=False)


# 見積・契約に共通する審査連携カラム（Mixin 化を推奨）
class AitmReviewMixin:
    aitm_transaction_id = Column(String(32), nullable=True, index=True)
    aitm_case_no        = Column(String(64), nullable=True, unique=True)
    aitm_review_type    = Column(String(16), nullable=True)   # provisional|formal
    aitm_status         = Column(String(32), nullable=True)
    aitm_revision       = Column(Integer, nullable=True)
    aitm_valid_until    = Column(DateTime(timezone=True), nullable=True)
    aitm_review_key_hash= Column(String(64), nullable=True)
    aitm_submitted_at   = Column(DateTime(timezone=True), nullable=True)
    aitm_judged_at      = Column(DateTime(timezone=True), nullable=True)

class Quote(Base, AitmReviewMixin):
    ...
    status              = Column(Enum(QuoteStatus), nullable=False)  # DRAFT|ISSUABLE|ISSUED|ACCEPTED|REJECTED|EXPIRED
    end_user_account_id = Column(ForeignKey("account.id"), nullable=True)   # ★
    end_user_certificate_status = Column(String(32), nullable=True)
    destination_country = Column(String(2), nullable=True)
    end_use             = Column(Text, nullable=True)
    fx_rate             = Column(Numeric(18, 6), nullable=True)
    fx_rate_date        = Column(Date, nullable=True)

class Contract(Base, AitmReviewMixin):
    ...
    source_quote_id     = Column(ForeignKey("quote.id"), nullable=True)
    end_user_account_id = Column(ForeignKey("account.id"), nullable=True)   # ★
    end_user_certificate_status = Column(String(32), nullable=True)
    erp_sales_order_number = Column(String(32), nullable=True, index=True)
    aitm_allocation_id  = Column(String(32), nullable=True)   # ライセンス仮引当
    # 実績3層
    shipped_amount      = Column(Numeric(18, 2), nullable=False, default=0)
    billed_amount       = Column(Numeric(18, 2), nullable=False, default=0)
```

## 9.2 `ComplianceStatus` の拡張

```python
class ComplianceStatusValue(str, Enum):
    UNKNOWN         = "UNKNOWN"          # 未判定・取得失敗 ← CLEAR と混同しないこと
    PENDING         = "PENDING"
    CLEAR           = "CLEAR"
    FLAGGED         = "FLAGGED"
    NEEDS_REVIEW    = "NEEDS_REVIEW"
    PENDING_LICENSE = "PENDING_LICENSE"
    OVERRIDDEN      = "OVERRIDDEN"       # ★ override により通過
    HIT             = "HIT"
    WITHDRAWN       = "WITHDRAWN"        # ★ 取下げ済み

class ComplianceStatus(Base):
    ...
    gate_kind           = Column(Enum(GateKind), nullable=False)  # ★ export | commerce
    account_id          = Column(ForeignKey("account.id"), nullable=True)
    quote_id            = Column(ForeignKey("quote.id"), nullable=True)      # ★
    contract_id         = Column(ForeignKey("contract.id"), nullable=True)   # ★
    party_role          = Column(String(16), nullable=True)   # counterparty|end_user
    status              = Column(Enum(ComplianceStatusValue), nullable=False)
    aitm_party_id       = Column(String(32), nullable=True)
    screening_result_id = Column(String(32), nullable=True)
    transaction_id      = Column(String(32), nullable=True)
    revision            = Column(Integer, nullable=False, default=0)
    hits                = Column(JSONB, nullable=True)
    red_flags           = Column(JSONB, nullable=True)
    rationale           = Column(Text, nullable=True)
    detail_url          = Column(String(512), nullable=True)
    override_info       = Column(JSONB, nullable=True)         # ★ IF-15 の内容
    judged_at           = Column(DateTime(timezone=True), nullable=True)
    valid_until         = Column(DateTime(timezone=True), nullable=True)
    updated_at          = Column(DateTime(timezone=True), nullable=False)
```

## 9.3 新規モデル

```python
# models/integration.py

class OutboxMessage(Base):
    """外部システムへの送信を保証する送信キュー"""
    __tablename__ = "outbox_message"
    id              = Column(UUID, primary_key=True)
    tenant_id       = Column(UUID, nullable=False)          # RLS対象
    target_system   = Column(String(16), nullable=False)    # 'aitm' | 'erp'
    kind            = Column(String(64), nullable=False)
    payload         = Column(JSONB, nullable=False)
    status          = Column(String(16), nullable=False)    # pending|sent|failed|dlq
    attempt_count   = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    last_error      = Column(Text, nullable=True)
    ref_type        = Column(String(32), nullable=True)
    ref_id          = Column(String(64), nullable=True)
    created_at      = Column(DateTime(timezone=True), nullable=False)
    sent_at         = Column(DateTime(timezone=True), nullable=True)


class WebhookEvent(Base):
    """受信 Webhook の冪等性管理。最低30日保持"""
    __tablename__ = "webhook_event"
    event_id        = Column(String(64), primary_key=True)
    tenant_id       = Column(UUID, nullable=False)
    source_system   = Column(String(16), nullable=False)
    event_type      = Column(String(64), nullable=False)
    payload         = Column(JSONB, nullable=False)
    received_at     = Column(DateTime(timezone=True), nullable=False)
    processed_at    = Column(DateTime(timezone=True), nullable=True)
    result          = Column(String(16), nullable=False)   # processed|duplicate|stale|error
    error           = Column(Text, nullable=True)


class ContractFulfillment(Base):
    """実績3層のうち出荷・請求・返品を記録"""
    __tablename__ = "contract_fulfillment"
    id                  = Column(UUID, primary_key=True)
    tenant_id           = Column(UUID, nullable=False)
    contract_id         = Column(ForeignKey("contract.id"), nullable=False)
    kind                = Column(String(16), nullable=False)  # shipment|billing|return
    erp_document_number = Column(String(32), nullable=False)
    product_code        = Column(String(32), nullable=True)
    quantity            = Column(Numeric(18, 3), nullable=True)
    amount              = Column(Numeric(18, 2), nullable=False)
    currency            = Column(String(3), nullable=False)
    posted_at           = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "kind", "erp_document_number",
                                       "product_code"),)


class DeemedExportActivity(Base):
    """みなし輸出の対象となる営業活動"""
    __tablename__ = "deemed_export_activity"
    id              = Column(UUID, primary_key=True)
    tenant_id       = Column(UUID, nullable=False)
    engagement_id   = Column(ForeignKey("engagement.id"), nullable=False)
    event_type      = Column(String(32), nullable=False)
    occurred_at     = Column(DateTime(timezone=True), nullable=False)
    technology_area = Column(String(255), nullable=True)
    participants    = Column(JSONB, nullable=False)   # 氏名・国籍・所属
    description     = Column(Text, nullable=True)
    aitm_event_id   = Column(String(32), nullable=True)
    risk_level      = Column(String(16), nullable=True)
```

- 全テーブルに既存のテナント分離方針にあわせ **Row-Level Security ポリシーを適用**すること

## 9.4 Alembic マイグレーション

```
alembic/versions/
  xxxx_add_integration_columns.py            # 9.1 の追加カラム
  xxxx_extend_compliance_status.py           # 9.2（Enum値追加）
  xxxx_create_integration_tables.py          # 9.3 ＋ RLSポリシー
  xxxx_add_quote_status_issuable.py          # QuoteStatus に ISSUABLE を追加
  xxxx_add_engagement_stage_rnd.py           # RND_INCUBATION ステージ
```

> **注意:** PostgreSQL の Enum に値を追加する `ALTER TYPE ... ADD VALUE` はトランザクション内で実行できません。マイグレーションを分割するか、`String` + `CheckConstraint` への移行を検討してください。

---

# 10. UI 変更

| 画面 | 変更内容 |
|---|---|
| **見積詳細** | **2系統のゲート表示**（輸出／商流）。審査ステータス・判定根拠・Red Flag。**「審査鮮度: あと N 日」**。override 情報。**ライセンス残枠の警告**。出力ボタンの活性制御（社内用／顧客提出用を分離） |
| **契約詳細** | `aitm_transaction_id` / `case_no` / ステータス / 判定根拠。ライセンス引当状況。**実績3層（契約額・出荷・請求）と受注残・消化率**。契約書出力ボタンの活性制御 |
| 商談詳細 `/ui/engagements/{id}` | 固定エリアにコンプライアンスバッジ（**契約相手とエンドユーザーの両方**）。継続監視アラート（IF-16）の表示 |
| **取引先詳細** | スクリーニング結果（`hits` 一覧・照合スコア・リスト名）、`aitm_party_id`、常時監視の有無、最終スクリーニング日時。**ERP 未登録の取引先である旨のバッジ**。ERP から付与された属性（与信枠・支払条件・顧客グループ） |
| **エンドユーザー管理** | 商談・見積・契約でエンドユーザーを選択・登録する UI。契約相手と同一の場合のショートカット |
| 商品マスタ `/ui/products` | `erp_material_code` の編集列、**未マッピング品目の一覧・件数バッジ**、ERP から同期された規制情報（ECCN/HS/原産国）の表示 |
| **活動ログ入力** | 「技術情報の授受を含む」フラグ、相手方参加者の国籍・所属の記録 |
| ダッシュボード | 「コンプライアンス要対応」カード（`NEEDS_REVIEW` / `HIT` / `UNKNOWN` の件数）。DLQ 滞留件数。**審査鮮度切れ間近の見積** |
| **新規: 連携ステータス画面** | Outbox の送信待ち・失敗・DLQ 一覧、手動再送、受信 Webhook 履歴 |
| **新規: R&D 起点商談** | `RND_INCUBATION` ステージの一覧。通常ステージへの昇格操作 |

---

# 11. 環境変数

```bash
# ---- AI_TM 接続先（★ハードコード禁止）----
AITM_PORTAL_URL=https://app.tsp-aitrademanagement.com
AITM_VALIDATION_URL=https://validation.tsp-aitrademanagement.com
AITM_CLASSIFICATION_URL=https://classification.tsp-aitrademanagement.com
AITM_SCREENING_URL=https://screening.tsp-aitrademanagement.com
AITM_RND_URL=https://rnd.tsp-aitrademanagement.com
AITM_LICENSE_URL=https://license.tsp-aitrademanagement.com

# ---- ERP 接続先 ----
ERP_BASE_URL=https://erp.example.com

# ---- 送信認証 ----
AITM_BEARER=<32byte以上のランダム値>
AITM_REQUEST_SIGNING_SECRET=<現行鍵>
ERP_BEARER=<...>
ERP_REQUEST_SIGNING_SECRET=<...>

# ---- 受信認証 ----
AITM_WEBHOOK_BEARER=<...>
AITM_WEBHOOK_SIGNING_SECRET=<現行鍵>
AITM_WEBHOOK_SIGNING_SECRET_PREVIOUS=<旧鍵。ローテーション期間中のみ>
ERP_WEBHOOK_BEARER=<...>
ERP_WEBHOOK_SIGNING_SECRET=<...>

# ---- 動作モード ----
AITM_INTEGRATION_MODE=live          # live | mock
ERP_INTEGRATION_MODE=live           # live | mock
COMMERCE_CHECK_MODE=stub            # ★ ERP側がダミー実装の間は stub

# ---- 業務パラメータ ----
SCREENING_FRESHNESS_DAYS=30
PROVISIONAL_REVIEW_VALID_DAYS=30    # 表示用。正は AI_TM の valid_until
REVIEW_KEY_VALUE_BUCKET_USD=100000
EXPIRED_REVIEW_REVERT_QUOTE=true    # 審査期限切れで見積を DRAFT に戻すか
OUTBOX_MAX_ATTEMPTS=6
OUTBOX_WORKER_INTERVAL_SEC=30
WEBHOOK_EVENT_RETENTION_DAYS=30
```

---

# 12. 実装タスクとフェーズ計画

## Phase 0 — 共通基盤

| ID | タスク | 依存 | 目安 |
|---|---|---|---|
| C0-1 | Alembic: 追加カラム・`ComplianceStatus` 拡張 | — | 3人日 |
| C0-2 | Alembic: `outbox_message` / `webhook_event` ＋ RLS | C0-1 | 2人日 |
| C0-3 | `SignedClient`（署名生成・共通ヘッダ） | — | 2人日 |
| C0-4 | `verify_webhook`（署名検証・リプレイ対策・鍵ローテーション） | — | 3人日 |
| C0-5 | Outbox ワーカー（バックオフ・DLQ） | C0-2, C0-3 | 4人日 |
| C0-6 | Webhook 冪等性・順序解決の共通ハンドラ | C0-2, C0-4 | 2人日 |
| C0-7 | 連携ステータス画面 | C0-5, C0-6 | 3人日 |
| | **小計** | | **19人日** |

## Phase 1 — 2段階審査とゲート制御

| ID | タスク | 依存 | 目安 |
|---|---|---|---|
| C1-1 | `Product.erp_material_code` の編集UI・未マッピング一覧 | C0-1 | 3人日 |
| C1-2 | `AITMValidationPort` ＋ Mock 実装 | C0-3 | 2人日 |
| C1-3 | `review_key_hash` の生成ロジック | C1-2 | 2人日 |
| C1-4 | **見積作成フック（仮審査の起票）** | C1-2, C1-3 | 3人日 |
| C1-5 | **契約発行フック（正式審査の起票）** | C1-4 | 2人日 |
| C1-6 | `409` / `422` のハンドリング・ActionItem 起票 | C1-4 | 2人日 |
| C1-7 | IF-10 受信ハンドラ（ステータス反映・ActionItem） | C0-6 | 3人日 |
| C1-8 | **`QuoteStatus.ISSUABLE` の追加と遷移制御** | C1-7 | 3人日 |
| C1-9 | **2系統ゲート（export / commerce）の実装** | C1-8 | 4人日 |
| C1-10 | **ドキュメント出力制御（サーバ側強制・DRAFT透かし）** | C1-9 | 5人日 |
| C1-11 | 審査鮮度の表示・期限切れの差し戻し | C1-7 | 2人日 |
| C1-12 | IF-15 override 受信・表示 | C0-6 | 3人日 |
| C1-13 | IF-08 審査の取下げ（失注・キャンセル連動） | C1-5 | 2人日 |
| C1-14 | 見積詳細・契約詳細のUI | C1-9, C1-12 | 5人日 |
| | **小計** | | **41人日** |

## Phase 2 — 取引先とエンドユーザー

| ID | タスク | 依存 | 目安 |
|---|---|---|---|
| C2-1 | `party_ref` 構造体・`build_party_ref` | C0-3 | 2人日 |
| C2-2 | **エンドユーザーのデータモデルと選択UI** | C0-1 | 4人日 |
| C2-3 | `AITMScreeningAdapter` の改修（両者対応・UNKNOWN扱い） | C2-1, C2-2 | 3人日 |
| C2-4 | **Account 作成時の自動スクリーニングフック** | C2-3 | 3人日 |
| C2-5 | 見積作成前の鮮度チェック・再スクリーニング | C2-4, C1-4 | 2人日 |
| C2-6 | IF-11 受信（`screening.alert` / `party.linked`） | C0-6 | 3人日 |
| C2-7 | IF-12 受信（既存実装の新スキーマ対応） | C0-6 | 3人日 |
| C2-8 | 商談・見積・契約の作成ブロック制御 | C2-6 | 2人日 |
| C2-9 | 取引先詳細UI（hits・ERP未登録バッジ・監視状態） | C2-6 | 3人日 |
| C2-10 | IF-02 バッチスクリーニング | C2-3 | 2人日 |
| | **小計** | | **27人日** |

## Phase 3 — ERP 連携

| ID | タスク | 依存 | 目安 |
|---|---|---|---|
| C3-1 | **IF-32 与信・反社チェックの送信（stub対応含む）** | C0-3 | 3人日 |
| C3-2 | `counterparty_attributes` の Account への反映 | C3-1 | 2人日 |
| C3-3 | IF-25 契約close時の受注転記 | C0-5 | 4人日 |
| C3-4 | IF-26 品目マスタ受信（`ErpMaterial` 同期） | C0-6 | 3人日 |
| C3-5 | IF-27 取引先マスタ受信 | C0-6 | 2人日 |
| C3-6 | **IF-29 / IF-30 実績受信と3層集計** | C0-6 | 5人日 |
| C3-7 | IF-31 返品受信（マイナス計上） | C3-6 | 2人日 |
| C3-8 | 契約詳細の実績3層UI・時系列分析 | C3-6 | 4人日 |
| C3-9 | IF-28 与信枠照会（見積時のリアルタイム参照） | C0-3 | 2人日 |
| | **小計** | | **27人日** |

## Phase 4 — ライセンス・R&D・みなし輸出

| ID | タスク | 依存 | 目安 |
|---|---|---|---|
| C4-1 | IF-06 ライセンス残枠照会・見積画面への警告 | C1-4 | 4人日 |
| C4-2 | IF-07 仮引当（契約発行時）・解放（キャンセル時） | C1-5, C1-13 | 3人日 |
| C4-3 | IF-14 R&D 商談の自動作成・`RND_INCUBATION` ステージ | C0-6, C2-4 | 4人日 |
| C4-4 | R&D 起点商談の一覧・昇格操作UI | C4-3 | 3人日 |
| C4-5 | IF-09 みなし輸出イベントの送信 | C0-3 | 3人日 |
| C4-6 | 活動ログUI（技術情報授受フラグ・参加者国籍） | C4-5 | 4人日 |
| C4-7 | IF-13 みなし輸出リスクの受信・表示 | C0-6 | 2人日 |
| C4-8 | IF-16 継続監視アラートの受信・更新商談ブロック | C0-6 | 3人日 |
| C4-9 | IF-04 / IF-05 品目・判定履歴の照会とキャッシュ | C0-3 | 3人日 |
| C4-10 | 見積品目追加時の規制事前警告 | C4-9 | 3人日 |
| C4-11 | ダッシュボードの各カード | C1-7, C3-6 | 3人日 |
| | **小計** | | **35人日** |

**総計: 約 149人日**（テスト工数を含む。レビュー・結合試験は別途）

---

# 13. テスト計画

## 13.1 単体テスト

| 対象 | 検証観点 |
|---|---|
| 署名生成 | 生成した署名を同じ鍵で検証すると一致する。`content=` と署名対象が同一バイト列 |
| 署名検証 | 改ざんボディで `401`／タイムスタンプ 301秒前で `401`／旧鍵で通過 |
| 冪等性 | 同一 `event_id` の2回目は `200 duplicate`、状態は変化しない |
| 順序解決 | `revision` が現在値以下のイベントは `stale` として破棄される |
| ステータスマッピング | AI_TM の全ステータスが正しく CRM 値に変換される |
| **フェイルクローズ** | **AI_TM / ERP がタイムアウトした際、`CLEAR` ではなく `UNKNOWN` になる** |
| **2系統ゲート** | 輸出ゲート PASS・商流ゲート BLOCK で見積が ISSUABLE にならない |
| **ドキュメント出力** | 未クリア時に顧客提出用PDFの**API を直接叩いても** 403 になる |
| **エンドユーザー** | 契約相手 clear・エンドユーザー HIT で商談作成がブロックされる |
| **審査キーハッシュ** | 数量変更でハッシュ変化／値引き5%（同一金額帯）でハッシュ不変 |
| **審査鮮度** | `valid_until` 経過で見積が DRAFT に差し戻される |
| 品目マッピング | 未設定の見積は Outbox に積まれず、ActionItem が起票される |
| USD換算 | `fx_rate` を用いた `total_value_usd` が明細合計と一致する |
| **実績3層** | 部分出荷・分割請求で受注残・未請求残が正しく算出される |
| **返品** | マイナス計上で出荷実績・請求実績が減算される |
| Outbox | `5xx` でリトライ、`422` で即 failed、6回失敗で `dlq` |
| R&D 商談 | `RND_INCUBATION` で作成され、パイプライン集計から除外される |

## 13.2 結合テスト（AI_TM・ERP チームと合同）

| # | シナリオ | CRM 側の期待挙動 |
|---|---|---|
| IT-01 | ERP 未登録の取引先を作成しスクリーニング | `Account.aitm_party_id` が保存される |
| IT-02 | 同じ法人が ERP に登録され `party.linked` を受信 | `external_system="erp"` / `external_id=bp_code` が自動設定される |
| IT-03 | 契約相手 clear・エンドユーザー match | 商談にリスクフラグが立ち、見積作成がブロックされる |
| IT-04 | 見積を DRAFT で作成 | 仮審査（IF-03）と与信・反社チェック（IF-32）が**両方**起票される |
| IT-05 | 両ゲート通過 | 見積が ISSUABLE になり、顧客提出用PDFが出力可能になる |
| IT-06 | 輸出ゲートのみ通過（商流 NG） | 見積は DRAFT のまま。顧客提出用PDFは出力不可 |
| IT-07 | AI_TM で override 承認（IF-15） | 「override により通過」が表示され、ゲートが解除される |
| IT-08 | 見積改訂（数量変更なし） | ハッシュ不変のため再審査されない |
| IT-09 | 契約発行（仮審査から10日後・条件同一） | 正式審査が即完了し、契約書が出力可能になる |
| IT-10 | 契約発行（仮審査から40日後） | フル再判定が走り、完了まで契約書は出力不可 |
| IT-11 | 見積時にライセンス残枠が不足 | 見積画面に警告とリードタイムが表示される |
| IT-12 | 契約 close → ERP 転記 | `aitm_transaction_id` が引き渡され、ERP が新規審査を起票しない |
| IT-13 | ERP で出荷 → IF-29 受信 | 契約の出荷実績が加算され、受注残が減る |
| IT-14 | ERP で invoice → IF-30 受信 | 請求実績が加算され、未請求残が正しくなる |
| IT-15 | 契約期間中の制裁ヒット（IF-16） | 契約にアラート、更新商談の起票がブロックされる |
| IT-16 | 商談を失注 | 審査取下げ（IF-08）とライセンス解放（IF-07）が送信される |
| IT-17 | R&D 案件から IF-14 受信 | 商談が `RND_INCUBATION` で自動作成される |
| IT-18 | AI_TM 停止中に見積作成 | 見積は作成できるがゲートは通過せず、Outbox に滞留・復旧後に送信される |
| IT-19 | 同一 `event_id` を2回受信 | 2回目は `200` を返し状態は変わらない |
| IT-20 | 古い `revision` のイベントを後から受信 | 破棄され、新しい状態が維持される |

## 13.3 障害・デグレードテスト

| シナリオ | 期待結果 |
|---|---|
| AI_TM 全停止中に商談〜見積〜契約を実施 | 業務は継続可能。ゲートはブロック。Outbox に滞留 |
| ERP 全停止中に見積作成 | 商流ゲートが `UNKNOWN` でブロック。輸出ゲートは通常動作 |
| Webhook が24時間届かない | 運用アラートが発報される |
| Outbox が DLQ に到達 | ダッシュボードに件数表示、手動再送で復旧できる |
| 鍵ローテーション中（新旧2鍵） | 旧鍵で署名されたリクエストも受理される |

---

# 14. 留意事項

1. **`UNKNOWN` を `CLEAR` にフォールバックしないこと。** 本連携で最も事故につながりやすい実装ミスです。外部システムの応答が得られないことは「問題なし」ではありません。テストケースで必ず担保してください。

2. **署名対象と送信ボディを必ず同一バイト列にすること。** `httpx` の `json=` 引数や辞書の再シリアライズによる署名不整合は、結合テストまで発覚しにくい典型的な不具合です。

3. **ドキュメント出力制御はサーバ側で強制すること。** 画面でボタンを隠すだけでは URL 直打ちで出力できてしまいます。輸出管理上、顧客提出物の管理は監査対象です。

4. **品目マッピングの棚卸しは開発と並行して進めてください。** `Product.erp_material_code` が未設定のままでは、Phase 1 が完成しても審査が起票されません。**技術課題ではなく業務準備の課題**であり、開発完了を待つと本番稼働が遅れます。

5. **エンドユーザーの管理は後付けが困難です。** データモデルの根幹に関わるため、Phase 2 で確実に入れてください。後から追加すると既存の商談・見積・契約への遡及対応が必要になります。

6. **IF-32 は ERP 側がダミー実装であることを前提に作らないこと。** 本番同等のインターフェースで呼び出し、レスポンスの `overall_result` を素直にゲートへ反映してください。**ERP 側の中身が入れ替わったときに CRM を改修しなくて済む状態**が目標です。`COMMERCE_CHECK_MODE=stub` はログ出力の抑制程度に留め、業務ロジックの分岐には使わないでください。

7. **Webhook のレスポンスに業務データを載せないこと。** 既存の `sanctions-list-updated` は Engagement 一覧を返す実装ですが、受理応答のみに変更します。AI_TM 側の実装と併せて調整が必要です。

8. **Webhook 由来の更新を監査可能にすること。** `actor_id = "aitm-webhook"` / `"erp-webhook"` として記録し、「誰がこの商談をブロックしたのか」を後から追跡できるようにしてください。CRM には RBAC がないため、監査ログが唯一の追跡手段になります。

9. **認証層（画面側）の整備は本連携のスコープ外ですが、本番投入の前提です。** 本連携の API・Webhook は独立した認証を持ちますが、CRM 本体は認証なしで稼働しています。リバースプロキシでの認証または VPN 限定アクセスの手当てを、情報システム部門と並行して進めてください。

---

# 15. 用語集

| 用語 | 意味 |
|---|---|
| `transaction_id` | AI_TM が採番する取引審査の内部ID。3システムを貫く共通キー |
| `case_no` | CRM が付与する審査番号。仮審査 `CRM-Q{quote_id}` / 正式審査 `CRM-C{contract_id}` |
| `aitm_party_id` | AI_TM が採番する取引先の共通識別子 |
| `review_type` | 審査種別。`provisional`（仮審査・見積時）/ `formal`（正式審査・契約発行時） |
| `review_key_hash` | 審査対象キー（品目・数量・仕向地・エンドユーザー・用途・金額帯）のハッシュ |
| 輸出ゲート | AI_TM 由来のリスク判定による進行制御 |
| **商流ゲート** | **ERP 由来の与信・反社チェックによる進行制御（本プロジェクトで新設）** |
| override | AI_TM 側でリスク判定を上書きする操作。CRM は結果と理由を表示するのみ |
| エンドユーザー | 製品を実際に使用する主体。契約相手とは異なりうる。**規制判定の核心** |
| みなし輸出 | 外為法上、外国籍の人物への機密技術の提供・開示を「輸出」とみなす概念 |
| 実績3層 | 契約額／出荷実績／請求実績。受注残・未請求残・消化率の算出基礎 |
| Outbox | 送信保証のためのキュー。失敗時にリトライし、全失敗で DLQ へ |
| DLQ | Dead Letter Queue。全リトライ失敗後にメッセージを退避する領域 |
| フェイルクローズ | 判定不能時に「安全側（＝進行不可）」に倒す設計方針 |

---

**改訂履歴**

| 版 | 日付 | 内容 |
|---|---|---|
| 1.0 | 2026-08-14 | 初版。2段階審査・2系統ゲート・エンドユーザー分離・実績3層・ライセンス連携・R&D起点商談を反映 |
