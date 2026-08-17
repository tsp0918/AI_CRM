# CRM開発チームへの引き継ぎ書 — ERP側IF-25対応を受けたCRM改修と残課題

作成日: 2026-08-17
対象: `crm_mvp/services/erp_transcription.py`(IF-25 契約→ERP受注転記)

## 概要

ERP側で `POST /crm/sales-orders`(IF-25専用エンドポイント)が新設されたことを受け、`erp_transcription.py` を実データで再検証・改修した。ERP側の対応で解消した問題と、CRM側の実装で対応した問題、そして**CRM側の設計判断が必要な未解決課題**を切り分けて記載する。

---

## 1. 今回CRM側で対応した内容(コード変更済み・テスト green)

### 1.1 新エンドポイント `POST /crm/sales-orders` への切り替え

- 旧 `/sd/sales-orders` はOAuth2パスワードフロー認証が必要で、トークンが約1時間で失効する問題があった。
- 新 `/crm/sales-orders` は `/crm/commerce-check` と同じ署名スキーム(`X-Timestamp` 等、実際には未署名でも通る)で、OAuth2トークンの取得・更新が一切不要になった。
- レスポンスの受注番号フィールド名が `document_number` → `erp_document_number` に変わったため、`Contract.external_id` への反映ロジックを追従させた。
- `document_date` フィールドが新スキームでは不要(ERP側で自動付与)になったため送信ペイロードから削除した。

### 1.2 `client_id` の不一致バグを発見・修正

**これが今回新たに見つかった実バグ。** 送信ペイロードに `"client_id": "AI_CRM"` を指定していたところ、実在する取引先(`SIM-BP-0000013` 等、`/mdm/business-partners` に確かに存在する)に対して `404 Customer (BusinessPartner) not found` が返っていた。

原因を切り分けたところ、`client_id="DEMO"` を指定すると同じ `customer_code` で正常に見つかることを確認した。P2で投入した取引先マスタはすべてERPの既定 `client_id="DEMO"` 配下に登録されており、`/crm/sales-orders` は `customer_code` の検索を `client_id` 単位でスコープしている(一方 `/crm/commerce-check` は `client_id` でスコープしていないため、この不一致に今まで気づけなかった)。

`erp_transcription.py` の送信ペイロードを `"client_id": "DEMO"` に修正し、実際に契約(`C-2026-0023`)のERP転記が成功することを確認した(`erp_document_number: 0010000226`)。

### 1.3 検証方法

前回のテストで不具合があった箇所(認証方式・`document_date`必須・新規顧客の`customer_code`欠如)を重点的に再確認し、新エンドポイントに対して実際にライブでリクエストを送って挙動を確認した。既存の自動テスト(`pytest`、631件)はグリーンのまま。

---

## 2. CRM側で未解決・設計判断が必要な課題

### 2.1 【最重要】CRM発生(ERP未登録)の新規顧客契約が、依然として受注転記できない

`/crm/sales-orders` の `customer_code` は引き続き必須で、未登録のコードを送ると `404` になる(空文字列や存在しないコードでの動作を再確認済み、ERP側の挙動としては妥当)。

CRMには現状、**新規Accountが作られた際にERPへBPを自動登録する仕組みが無い**。そのため、Web UIの `engagement_new_submit`(常に新規Accountを作る)経由で作られた商談は、契約を締結(SIGNED)してもERP転記が構造的に必ず失敗する。異常検出 `A-03`(契約close済みだがERPに受注が存在しない案件)として実データで検出済み。

**CRM側で検討すべき対応案**(いずれも未実装、設計判断が必要):
- (a) 契約SIGNED時に `customer_code` が無いAccountを検知したら、ERPへBP新規登録(`POST /mdm/business-partners`)を先に行ってから受注転記する経路を`erp_transcription.py`に追加する
- (b) Account作成(またはengagement作成)の時点でERP BP登録を行う経路を別途用意する
- (c) 上記どちらも実装しないなら、少なくとも「ERP未登録の取引先とは契約を締結させない」ゲート(`evaluate_artifact_gate`)を追加し、A-03のような「後から気づく」状態を防ぐ

いずれの案も画面フロー・エラーメッセージ設計に関わるため、次のスプリントで方針を決めてから着手することを推奨する。

### 2.2 `client_id` の使い分けが暗黙的

`commerce_check.py` は `"client_id": "AI_CRM"` を使い続けているが、これは `/crm/commerce-check` が `client_id` でスコープしていないために表面化していないだけで、**ERP側が将来 `client_id` スコープを追加した場合に同種のバグが再発するリスク**がある。

`AI_CRM` という独自client_idをERP側に正式採番してもらう(その場合はマスタ投入時のBP登録もそのclient_id配下で行う必要がある)か、CRM側で `DEMO` に統一するか、ERPチームと合わせて方針を決めることを推奨する。

### 2.3 ERP→CRMへのwebhook配信は引き続き未接続(環境設定の話、コードの話ではない)

`.env` の `CRM_WEBHOOK_BASE_URL` 等が未設定のため、出荷・請求実績のCRMへの自動反映は動いていない。これは以前から把握済みの既知事項で対応要否は判断待ちのまま。

### 2.4 ERP側の残存バグ(参考、CRM側の対応は不要)

`aitm_transaction_id` を省略して `/crm/sales-orders` を呼ぶと、ERP内部で `'TransactionCreateResponse' object has no attribute 'ai_status'` という未処理エラーになる。CRM側は正式審査完了後に必ずこの値を渡す設計になっているため実害は無いが、ERPチームへの報告事項として記録しておく。

---

以上、次の対応(2.1〜2.2)についてはCRMチーム内で方針を決めていただきたい。
