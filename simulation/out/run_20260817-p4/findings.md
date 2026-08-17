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
# findings.md(P4 本実行・コア経路、2026-08-17)

`docs/BULK_SIMULATION_SPEC.md` §10.3のP4完了条件("完走し、out/run_<id>/に
4ファイルが生成される。APIエラーは記録されていてよいが、未説明のエラーが
残っていないこと")のうち、**コア経路(商談60件 → 見積 → 発行できれば契約
→ 締結できればERP転記・出荷・請求)**を実3システムに対して実行した結果。
ユーザー承認によりこの回では対象外としたもの: 見積の複数回改訂、審査
鮮度切れ再現(§11.3)、継続監視ヒット・みなし輸出・返品・ERP単独受注・
R&D起点(IF-14/26)。件数の生成自体はP1で確定済み(dry-run 60/90/35/69一致)。

## 1. 実行結果サマリー(60商談)

| 段階 | 結果 |
|---|---|
| 見積 issuable | 26件 |
| 見積 blocked_review(輸出管理needs_review) | 29件 |
| 見積 blocked_party(制裁ヒット) | 3件 |
| 見積 error(transaction_id引き当て失敗) | 2件 |
| 契約 signed | 4件 |
| 契約 blocked_review | 7件 |
| 契約 erp_transcription_failed(新規顧客ERP未登録、既知のギャップ) | 7件 |
| 出荷・請求 completed | 4件 |

`verify/reconcile.py`(R-01/R-02/R-03/R-08)・`verify/anomaly.py`(A-03/A-07/A-08)を
全件に対して実行。**A-03(契約SIGNED済みだがERP受注なし)が8件検出され、
すべて上記の既知ギャップ(新規顧客のERP未登録)に一致することを確認**。
それ以外(R-01/R-02/R-03/R-08/A-07/A-08)はすべてパス — 60件規模でも
審査の重複無し・アカウント/品目のID整合性・フェイルクローズ違反無し・
孤児レコード無しを確認できた。

## 2. 新たに発見・修正したバグ(P4実行準備・実行中に4件)

| # | ファイル | 内容 | 修正 |
|---|---|---|---|
| 1 | `simulation/src/db.py`(シミュレータ側) | `tenant_session()`が呼び出しのたびに`create_engine()`しており、1商談あたり10回前後×60商談でPostgreSQLの接続上限を使い切り、実行が完全に停止した(`FATAL: remaining connection slots...`) | engineをプロセス内で使い回すよう変更(pool_size=5, pool_pre_ping=True) |
| 2 | `simulation/src/clients/erp.py`(シミュレータ側) | ERPのOAuth2トークンは実測1時間弱で失効する。60件規模の実行はそれを超えうる | 15分ごとに自動再取得するよう変更 |
| 3 | `simulation/src/clients/crm_ui.py`(シミュレータ側) | 明細数量をfloat文字列("10.0")で送っていたが、CRMのフォームは`int(quantity)`でパースするため全件エラーになっていた | 整数文字列に変換して送るよう修正 |
| 4 | `simulation/src/clients/crm_ui.py`(シミュレータ側) | `_post()`がリダイレクト先の`flash_type`しか見ておらず、CRM側の500エラー(未処理例外)を検知できず、後続処理で`NoneType`エラーとして誤って現れていた。実際の原因は`Quote.destination_country`が`VARCHAR(2)`なのに、シナリオ生成側の分布ラベル"OTHER"をそのまま国コードとして送っていたこと(`p4_run.py`側のデータ不整合) | ①`_post()`にHTTPステータスコードチェックを追加(500等を即座に検出) ②"OTHER"を実在の国コード("DE")へ写像 |

いずれもシミュレータ自身のバグで、CRM本体には手を入れていない。#4はエラー検知が甘かったために「原因不明のNoneType例外」に見えていたケースで、根本原因の特定にHTTPステータスチェックの追加が必須だった。

## 3. 設計上の学び(バグではないが重要な観察)

- **§4.4のウォッチリスト対象取引先(Alpha/Beta/Gamma/Delta/Epsilon/Zeta、計6社)が、通常の「既存顧客」account poolからも無差別に選ばれる。** `masters.py`はこれらを既存顧客プールに混在させて生成しており(§4.1の設計通り)、quadrantに基づく取引先選定(`_pick_account`)がプール全体から一様に選ぶため、本来は例外シナリオ専用のはずの取引先が通常商談にも頻繁に登場する。これにより、見積のneeds_review率が§6.2の想定(90件中18件=20%)よりかなり高く出た(60件中29件がblocked_review)。件数の生成自体(P1のdry-run)は§6.2の設計値通りだが、**「どの取引先を通常商談に使うか」の選定ロジックに、例外専用取引先を除外する仕組みが無い**ことが原因。次回改善するなら、通常商談用と例外専用で取引先プールを分けるべき。
- `find_transaction_id_by_case_no`(`/api/transactions/recent`を線形探索)が60件中2件で対象を見つけられなかった(取得件数の上限に収まらなかった可能性)。低頻度だが、medium規模以上ではより頑健な実装(直接case_noで検索できるエンドポイントが無いか確認する等)が必要。

## 4. 今回のパスで実行しなかったもの(P1では件数を確定済み)

- 見積の複数回改訂とreview_key_hash不変時の再審査省略の実証
- 審査鮮度切れ(§11.3、30日鮮度 vs 圧縮時間実行)の再現
- 継続監視ヒット(IF-12/16/24)・みなし輸出(IF-09/13)・返品(IF-22/31)・
  ERP単独受注(IF-17〜19/23)・R&D起点商談(IF-14/26)

## 5. 未着手

- P5: KPIレポート(§8.2)・最終`report.html`・`findings.md`の統合。
