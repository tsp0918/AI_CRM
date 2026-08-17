# findings.md(P0〜P3 スモーク時点、2026-08-17)

`docs/BULK_SIMULATION_SPEC.md` P0〜P3(スモーク)実行時点で判明した事項。
P4(medium規模本実行)・P5(KPI・最終レポート)は未実施。

## 1. CRM側で発見・修正した実バグ(3件)

| # | ファイル | 内容 | 修正 |
|---|---|---|---|
| 1 | `crm_mvp/services/erp_transcription.py` | ERP `/sd/sales-orders` が必須とする`document_date`がIF-25送信ペイロードに欠落し、常に422で失敗していた(実ERPと一度も疎通確認していなかったため未発覚) | `document_date`(転記実行時の実時刻)を追加 |
| 2 | `crm_mvp/services/erp_transcription.py` | `/sd/sales-orders`はHMAC署名方式ではなく、ERP自身のOAuth2パスワードフロー認証を要求する(`/crm/commerce-check`等のB2B署名エンドポイントとは別方式)。`SignedClient`で送っていたため常に401 | OAuth2トークンを`/auth/token`から都度取得して`Authorization: Bearer`に使うよう変更 |
| 3 | `crm_mvp/api/erp_webhooks.py` | ERPからのIF-29/30/31 webhook受信時、JSON由来のfloatをDecimal変換せずORMにそのまま渡していたため、同一契約で2件目以降の実績計上時に`TypeError`でクラッシュ | `Decimal(str(...))`で明示変換 |

いずれも「実際に3システムを繋いで動かして初めて分かる」種類の不具合で、単体テストや型チェックでは検出できなかった。

## 2. AI_TM側の実際の挙動(CRM側の設計前提とのズレ)

- **AI判定は自動実行されない**。`POST /api/crm/provisional-review` / `formal-review`は審査ケースを起票するだけで、`status`は`draft`のまま止まる。実際には
  1. `POST /ui/transactions/{id}/run-screening`(取引先スクリーニング)
  2. `POST /decision/{id}/run-and-two-lists`(該非二法令リスト照合、ここでtierが確定し`status`が`approved`等に変わる)
  の2段階を明示的に呼ぶ必要がある。CRM側の実装(および元の連携設計)は「送信すれば非同期で判定される」ことを前提にしていたが、実システムはそうなっていない。
- **AI_TM→CRMへのcallbackが存在しない**。判定結果を`/webhooks/aitm/review-result`へ能動的に送ってくる仕組みはAI_TM側に無い(2026-08-16のE2E疎通確認時点でも「未検証・未接続」と分かっていた点が、今回スモークで確定した)。
- **ウォッチリストは`/api/watchlist`への登録直後は検索に反映されない**。`/api/rebuild-index`を呼ぶまで`/api/screen`が新規エントリをヒットさせない。P2投入スクリプトに再構築ステップを追加済み。
- **完全一致する社名は`possible_match`ではなく`match`になる**。§4.4のウォッチリスト名をそのままAccountの`legal_name`に使うと、スコアが高くなり`match`(HIT)判定になる。`possible_match`(NEEDS_REVIEW)を再現するには、あえて表記ゆれのある社名を使う必要がある。
- **ライセンス不要品目でも`/api/licenses/allocations`は`{"allocation_id": null, "allocations": []}`を返し、CRM側はこれを`ALLOCATED`として記録する**。挙動としては合理的だが、「本当に引当された」のか「そもそも不要だった」のかがCRMのDB上では区別できない(`LicenseAllocationStatus`にNOT_REQUIRED相当が無い)。実害は今のところ無いが、KPI集計(§8.2「ライセンス枠の消費率」)の際は注意が必要。

## 3. 未解決の設計ギャップ(既知の欠落機能、今回は修正しない)

- **CRM発生(ERP未登録)の新規取引先は、契約SIGNED後のERP転記(IF-25)が必ず失敗する**。`customer_code`(ERPのBPコード)が無いため。これは§6.1の「既存プロダクト×新規顧客」「新規プロダクト×新規顧客」象限(商談60件中18件)で必ず起きることを意味する。ERPへの新規BP自動登録ステップがどこにも実装されていない。
  → `verify/anomaly.py`のA-03(CRMで契約close済みだがERPに受注が存在しない案件)が実際にこれを検出することをスモークで確認済み。
- `crm_mvp/api/deps.py`の`get_screening_port()`は常に`MockScreeningAdapter`(常にCLEARを返す)を使う。実`AITMScreeningAdapter`への差し替えは**本セッション以前からのドキュメント化済みの意図的な未実装**(`compliance_screening.py`冒頭コメント)。シミュレータはこれを迂回し、`/api/screen`を直接呼んでComplianceStatusを書く専用の橋渡し(`simulation/src/flows/party_screening.py`)で代替している。CRM本体側の恒久対応は別途の設計判断が必要。

## 4. P3スモーク実行結果(商談3件)

| シナリオ | 内容 | 結果 |
|---|---|---|
| A | 既存顧客(ERP登録済)× ライセンス必要品目(3C001) | 見積→審査→契約SIGNED→ERP転記→出荷→請求まで完走 |
| B | 新規顧客(Web UI経由で新規Account作成)× ライセンス不要品目 | 見積→審査→契約SIGNEDまで成功。ERP転記は上記§3の設計ギャップにより失敗(想定通り、A-03で検出) |
| C | 制裁ヒット取引先(実AI_TMスクリーニングで`match`と確定した実在の取引先) | 見積作成が`check_party_clearance`で正しくブロックされることを確認 |

`verify/reconcile.py`(R-01/R-02/R-03/R-08)・`verify/anomaly.py`(A-03/A-07/A-08)を実装し、上記シナリオに対して実行。既知のA-03以外は全てパス。R-04〜R-07/R-09/R-10・A-01/A-02/A-04〜A-06はP4(medium規模)で分納・失注・ERP単独受注等のデータが揃ってから実装・検証する。

## 5. 未着手

- P4: `--scale=medium`(商談60件・12ヶ月分)の本実行。P1のイベント生成は完了しているが、上記2節の実挙動(AI判定の明示トリガー必須・callback無し)を踏まえてP4の実行ループ(§3.5 `process_pending_judgments`)を作り込む必要がある。
- P5: KPIレポート(`report.html`)・最終`findings.md`。
