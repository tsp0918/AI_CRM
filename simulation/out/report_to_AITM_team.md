# AI_TM(AI Trade Management)連携チームへの報告書

作成日: 2026-08-17
作成元: AI_CRM側「3システム横断業務フローシミュレーション」(`docs/BULK_SIMULATION_SPEC.md`)
対象: `ai_validation`(取引審査, port 8011)・`screening`(取引先スクリーニング, port 8005)・`export_license`(輸出許可証, port 8012)

## 概要

CRM(AI_CRM)からAI_TMの実APIを実際に呼び出し、取引審査・取引先スクリーニング・輸出許可証枠の一連のフローを、商談60件規模で実行しました。API自体は概ね正しく動作しましたが、**CRM側が当初想定していた「非同期でAI判定が自動的に走る」という前提と、AI_TMの実際の挙動に差異があり**、CRM側の実装をAI_TMの実際の挙動に合わせて調整しています。以下、AI_TM側でのご確認・ご検討をお願いしたい事項です。

---

## 1. 取引審査の判定が自動実行されない(最重要)

`POST /api/crm/provisional-review` および `POST /api/crm/formal-review` は、審査ケース(transaction)を起票するだけで、**AI判定(スクリーニング・該非二法令リスト照合)は自動的には走りません**。起票直後に `GET /api/crm/review-status/{case_no}` を呼んでも、`status` は `draft` のまま、`agent_judgment_status` は `null` のままです。

実際に判定を完了させるには、以下2段階を明示的に呼ぶ必要があると分かりました。

1. `POST /ui/transactions/{id}/run-screening` — 取引先スクリーニングの実行
2. `POST /decision/{id}/run-and-two-lists` — 該非二法令リスト照合。ここで初めてtier(自動承認/要人手)が確定し、`status` が `draft` → `approved` 等に変わる

**確認したいこと**: これは意図した設計(CRM側またはオペレータ側が明示的にトリガーする前提)でしょうか、それとも本来はバックグラウンドジョブ等で自動的に実行される想定だが未接続なのでしょうか。前者であれば、CRM側の連携仕様書(またはAI_TM側のAPI仕様書)にその旨を明記いただけると、今後の連携実装で同じ手戻りを避けられます。

なお `transaction_id`(内部数値ID)は `/api/crm/*-review` のレスポンスに含まれていないため、`case_no` から `id` を引くのに `GET /api/transactions/recent` を線形探索する形で対応しました。60件中2件、対象のtransactionが取得できないケースがありました(直近リストの取得件数上限に収まらなかった可能性があります)。`case_no` で直接検索できるエンドポイント(または `/api/crm/*-review` のレスポンスに `transaction_id` を含めていただくこと)があると、CRM側の実装がより頑健になります。

## 2. AI_TM → CRMへの判定完了通知(webhook)が存在しない

判定完了後、AI_TMからCRMの `/webhooks/aitm/review-result` へ能動的に通知が来る仕組みが見当たりませんでした。今回のシミュレーションでは、①上記の判定実行を呼ぶ → ②`GET /api/crm/review-status/{case_no}` で結果を取得 → ③CRM側のwebhookエンドポイントへシミュレータ自身が代理で通知、という橋渡しを行って動作確認しました。

実運用でCRM側が判定完了を能動的に受け取る必要がある場合、AI_TM側での判定完了時のwebhook送信(CRM連携仕様書のIF-10相当)の実装状況をご確認いただけますでしょうか。

## 3. ウォッチリスト登録直後は検索に反映されない

`POST /api/watchlist` でエントリを登録した直後に `POST /api/screen` で照会しても、新規登録分がヒットしませんでした。`POST /api/rebuild-index` を呼んで初めて反映されます(`GET /api/index-status` の `ntotal` が更新されることで確認)。

運用上、ウォッチリスト更新の都度インデックス再構築が必要という理解で合っていますでしょうか。もし自動再構築のスケジュールが別途あるのであれば、その周期を教えていただけると、CRM側で「登録してすぐスクリーニングしても安全か」の判断に役立ちます。

## 4. 完全一致する社名は`match`(HIT)、`possible_match`ではない

テスト用ウォッチリスト作成時、意図的に「表記ゆれのある類似名」で `possible_match`(部分一致・要確認)を再現しようとしましたが、ウォッチリストの `entity_name` と完全一致する社名で照会すると、スコアが高くなり `match`(確定ヒット)として扱われることが分かりました(例: スコア0.96)。これはスコアリングの仕様として妥当だと思われますが、念のため共有します。

## 5. ライセンス不要品目でも `allocated` として応答される

`POST /api/licenses/allocations` に、許可証マスタに存在しない(=ライセンス不要と推定される)品目コードを送ると、以下のように応答されます。

```json
{"allocation_id": null, "status": "allocated", "allocations": [], "valid_until": "..."}
```

`status: "allocated"` かつ `allocations: []`(実際の引当は0件)という組み合わせで、「本当に引当された」のか「そもそも不要だった」のかを呼び出し側で区別できません。CRM側では現状この応答を「ALLOCATED」として記録していますが、可能であれば `status` に `not_required` のような値を用意いただくか、`allocations` が空の場合は別のフラグを立てていただけると、呼び出し側でより正確な状態管理ができます。

## 6. 正常に動作した点(参考)

- `POST /api/counterparties`(取引先登録)、`POST /api/screen`(スクリーニング)は仕様通り動作しました。
- ライセンス枠不足の検知は実データで正しく機能しました。300枠の許可証(SIM-LIC-02)に対し500個の引当を要求したところ、`{"code": "QUOTA_SHORTFALL", "message": "許可証 SIM-LIC-02 の残枠が 200 不足します。新規申請の想定リードタイムは 8 週間です。"}` という正確な警告を受け取れました。
- `POST /api/licenses/quotas/register` での許可証枠登録、`destination_country` 単位での枠管理も想定通り動作しました。

---

以上、ご確認のほどよろしくお願いいたします。詳細な実行ログ・再現手順が必要な場合はご連絡ください。
