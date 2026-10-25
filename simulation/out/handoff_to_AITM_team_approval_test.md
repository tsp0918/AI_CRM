# AI_TM開発チームへの引き継ぎ書 — 要承認ケースの実地テスト

作成日: 2026-08-17
作成元: AI_CRM側「3システム横断業務フローシミュレーション」

## 概要

CRM側で実際に新規案件を起案し、取引審査(仮審査)をAI_TMへ実送信(hook)しました。意図的にウォッチリストの類似名(表記ゆれ)を持つ取引先を使い、`possible_match`判定 → 人手承認が必要な状態を再現しています。**この案件について、AI_TM側で承認作業を実施してください。** 完了後にCRM側の後続処理(見積SENT遷移)を実行します。

## 対象ケース

| 項目 | 値 |
|---|---|
| AI_TM取引ID(`transaction_id`) | **441** |
| ケース番号(`case_no`) | **CRM-PROV-202608-812951** |
| 審査種別 | 仮審査(provisional) |
| 取引先名 | SIM Materials Gama Trading Ltd(架空企業) |
| 仕向地 | JP |
| 現在のステータス | `requires_review` |
| tier | 3(輸出許可確認) |
| tier判定理由 | スクリーニングにヒットあり → 輸出許可要否の確認が必要 |

## スクリーニング結果

`GET /api/transactions/441` および `GET /api/transactions/recent` で確認:

```
screening_status: "possible_match"
screening_result_id: "ea4a6b33-a28a-4468-8cb0-73eba7dde8c2"
```

ウォッチリスト上の `SIM Materials Gamma Ltd`(SIM_TEST_WATCHLIST登録済み、架空企業)に対してスコア0.77で部分一致しています。完全一致(HIT)ではないため、CRM側のゲートでは自動ブロックされず、AI_TM側の人手判定待ちの状態でCRMの見積が止まっています。

## AI_TM側で実施いただきたいこと

`GET /decision/441/review-checklist` で確認したチェックリストです。

```json
{
  "tier": 3,
  "tier_label": "輸出許可確認",
  "required_steps": ["screening", "ai_run", "catchall"],
  "required_done": false,
  "checklist": {
    "screening": {"done": true},
    "ai_run": {"done": true},
    "catchall": {"done": false, "label": "キャッチオール自己判定"},
    "formal_submitted": {"done": false}
  }
}
```

`screening`・`ai_run`は既に完了しています。**未完了なのは「キャッチオール自己判定」の1点**です。

### 手順案

1. スクリーニング結果を確認: `screening_result_id: ea4a6b33-a28a-4468-8cb0-73eba7dde8c2`(possible_matchが実際に無関係な誤検知か、要確認事項かを判断)
2. キャッチオール自己判定を実行: `POST /decision/441/catchall-judgment`

   ```json
   {
     "session_id": "<おそらくrun-and-two-listsのrun_idから取得。ai_tm側で採番方法をご確認ください>",
     "destination_country": "JP",
     "end_user_name": null,
     "end_user_country": null,
     "consignee_name": null,
     "inform_received": false,
     "euc_obtained": false,
     "end_use_description": "国内向け販売テスト(AI_TM承認テスト用)",
     "red_flags": {},
     "evaluator_note": "CRM側E2Eテスト: possible_matchはウォッチリスト上の類似企業(SIM Materials Gamma Ltd)との表記ゆれによる誤検知と判断"
   }
   ```

   Red Flag 7項目(輸出令第4条キャッチオール判定用)の具体的な入力内容は、AI_TM側の判定ロジックに合わせて適切な値を設定してください。

3. 該当なし(誤検知)と判定できる場合は、そのまま`status`が`approved`等へ遷移するか、または`POST /api/transactions/441/overrides`での例外承認(scope="transaction"、`department_head_approval`等)が必要か、AI_TM側の設計に従って判断してください。

## AI_TM側の対応が完了したら

以下を教えてください。CRM側で後続処理(判定結果をCRMの`ReviewCase`へ反映し、見積をSENTへ遷移させる)を実行します。

- 最終的な`status`(`approved` / `rejected` 等)
- 該当する場合は`valid_until`(審査有効期限)

---

参考: `GET /api/crm/review-status/CRM-PROV-202608-812951` で現在のステータスをいつでも確認できます。

---

## 実施結果(2026-08-17〜18)

### 1. キャッチオール自己判定の実施

AI_TM側で `POST /decision/441/catchall-judgment` を実行。結果は「承認」ではなく次の判定でした。

```
verdict: "requires_permit"(輸出許可申請 必須)
verdict_detail: インフォーム通知を受領しているため、輸出令第4条の規定により
                本取引の輸出許可申請が義務付けられる
reasons: INFORM_RECEIVED(経済産業省からのインフォーム通知受領)
recommended_actions:
  - 速やかに安全保障貿易管理部門へ報告
  - 経済産業省への輸出許可申請を準備
  - 取引を一時中断し、許可取得前の出荷を行わない
  - インフォーム通知の内容を取引記録として保管
```

`required_done: true`(screening/ai_run/catchallすべて完了)になった後も、`GET /api/crm/review-status`の`status`は`requires_review`のまま変化せず。この時点でCRM側の後続処理は「意図的に」実行せず待機(見積は正しくブロックされたままにした)。

### 2. 許可証承認とステータス不整合の発見

AI_TM側で許可証(export license)を承認。`GET /api/transactions/441`には

```
linked_license_id: "2e023c86-e2c6-45c8-bc38-f931862fa4b0"
linked_license_status: "approved"
```

が現れたが、**CRM向けの`GET /api/crm/review-status`の`status`は`requires_review`のまま変化せず**、許可証承認と審査ケース自体のステータス遷移が別プロセスになっている不整合を発見。この状態でCRM側へ反映すると、実際には許可証が下りているのに見積がブロックされ続けるという食い違いになるため、AI_TM側へ差し戻して確認を依頼した。

### 3. AI_TM側の修正・最終確認

AI_TM側で対応後、`GET /api/crm/review-status`の`status`が`"approved"`に変わったことを確認。

### 4. CRM側の後続処理・最終結果

- `flows/review.py`経由でAI_TMの最終判定(`approved`)をCRMの`/webhooks/aitm/review-result`へ橋渡し(`revision=2`、初回のneeds_review通知から進めるため)
- CRM側`ReviewCase.status`が`clear`に更新されたことをDBで確認
- 見積(`Q-2026-0069`)のSENT遷移を実行 → **成功**(`quote -> SENT: OK`)

新規案件の起案 → AI_TM審査hook → possible_match検出 → 要人手判定(輸出許可確認tier)→ キャッチオール自己判定 → 輸出許可要件の検出 → 許可証承認 → 審査ケースのステータス不整合の発見・修正 → CRM側への正式反映 → 見積発行、という一連のフローを実システム間で完走できることを確認した。
