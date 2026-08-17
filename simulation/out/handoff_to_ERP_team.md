# ERP開発チームへの引き継ぎ書 — IF-25(`/crm/sales-orders`)再検証で判明した課題

作成日: 2026-08-17
作成元: AI_CRM側「3システム横断業務フローシミュレーション」
対象: `POST /crm/sales-orders`(IF-25、契約→受注転記の新エンドポイント)

## 概要

`POST /crm/sales-orders` の新設(IF-25専用エンドポイント化・OAuth2認証不要化)を確認し、CRM側の連携コードを対応させたうえで再検証しました。認証方式・`document_date`必須の2点は解消を確認できましたが、**実際にCRMの契約データを流す過程で新しい不具合を1件、および設計判断が必要な課題を2件**発見しましたので共有します。CRM側で対応可能な範囲は既に着手済みです。

---

## 1. 【要修正】`aitm_transaction_id` 省略時に未処理例外が発生する

### 再現手順

```bash
curl -X POST http://localhost:8888/crm/sales-orders \
  -H "Content-Type: application/json" \
  -H "X-Timestamp: $(date +%s)" \
  -d '{
    "crm_contract_id": "test-001",
    "customer_code": "<実在するBPコード>",
    "currency": "JPY",
    "items": [{"material_code": "<実在する品目コード>", "quantity": 5, "unit_price": 15000}]
  }'
```

`aitm_transaction_id` フィールドを含めずに(または `null` で)呼び出すと、`201 Created` は返るものの受注が `status: "BLOCKED"` かつ `export_check_status: "ERROR"` になり、`export_check_message` に以下が入ります。

```
Integration error: 'TransactionCreateResponse' object has no attribute 'ai_status'
```

エンドポイントの説明文には「`aitm_transaction_id` が無い場合はAI_TMへ新規審査を起票する」とあるため、その起票時のレスポンス処理(おそらく `TransactionCreateResponse` を受け取ってから存在しない `ai_status` 属性にアクセスしている箇所)にバグがあると推測します。

**影響範囲**: CRM側は正式審査(formal review)完了後に必ず `aitm_transaction_id` を渡す設計のため、CRM→ERPの通常フローでは踏みません(`aitm_transaction_id` を指定した場合は `status: "OPEN"`, `export_check_status: "PENDING"` で正常に動作することを確認済みです)。ただし、CRM以外の呼び出し元(手動起票・他システム連携等)がこのパラメータを省略した場合に必ず踏む状態です。

---

## 2. 【ご検討ください】`customer_code` の事前登録が必須で、新規顧客の受注が作成できない

`customer_code` は必須で、未登録のコードを送ると `404 Customer (BusinessPartner) not found` になります(妥当な挙動だと思います)。

一方で `end_user` フィールドは `CrmEndUserPayload`(`name`・`country`等)を渡すことでBPを都度作成できる設計になっています。**もし主要取引先(`customer_code`)側にも同様の「未登録なら渡された情報でBPを自動作成する」オプションを追加いただけると**、CRM側で「まだERPに登録されていない新規顧客」の契約を、事前の別リクエストなしで1回の `POST /crm/sales-orders` で完結できるようになります。

現状CRM側では、新規に作成された取引先(CRM発生・ERP未登録)の契約はこのエンドポイントを呼ぶ前提が満たせず、受注登録が一貫して失敗する状態になっています。ERP側で対応いただける場合は、例えば以下のような形を想像しています(あくまで一案です)。

```json
{
  "customer_code": null,
  "customer": {"name": "...", "country": "JP", "address": null},
  "...": "..."
}
```

対応が難しい場合は、CRM側で契約締結前にBP登録リクエストを別途投げる設計に倒しますので、その場合は `POST /mdm/business-partners` を機械間連携(署名スキーム、OAuth2不要)から呼べるようにしていただけると助かります(現状このエンドポイントはOAuth2認証必須のため、CRM側は通常業務ユーザーの認証情報を使い回す必要があり不自然です)。

---

## 3. 【ご確認ください】`client_id` によるスコープの有無が endpoint ごとに異なる

`/crm/sales-orders` は `customer_code` の検索を `client_id` 単位でスコープしているようです(`client_id="DEMO"` では見つかる取引先が、`client_id="AI_CRM"` では `404 not found` になることを確認しました — 同じデータベース上に存在するにもかかわらず)。

一方 `/crm/commerce-check` は `client_id` を受け取りますが、スコープには使っていないようです(どの `client_id` を送っても同じ結果が返ります)。

CRM側は現状 `commerce-check` には `"AI_CRM"`、`sales-orders` には `"DEMO"` と、エンドポイントによって異なる `client_id` を送るよう暫定対応しました。本来は連携先ごとに一貫した `client_id` を使うべきだと考えます。以下のいずれかをご検討いただけますでしょうか。

- CRM用に正式な `client_id`(例: `"AI_CRM"`)を採番し、マスタ登録もその配下で行う運用にする
- 全エンドポイントで `client_id` スコープの有無を統一する(現状の非対称性を解消する)

---

## 4. 参考: 今回確認できた「解消済み」の項目

- `material_code` の重複登録防止(`409 Conflict`)が機能していることを再確認しました。
- `POST /crm/sales-orders` は `document_date` を送らなくても動作し、OAuth2トークンなし(HMAC署名スキームの範囲内)で呼び出せることを確認しました。
- `aitm_transaction_id` を正しく渡した場合の受注作成は問題なく動作しています(`status: "OPEN"`, `export_check_status: "PENDING"`)。

---

再現に必要な情報(実際に使ったBPコード・品目コード等)が必要な場合はお知らせください。
