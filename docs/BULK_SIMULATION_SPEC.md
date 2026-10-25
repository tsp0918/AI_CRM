# 3システム横断 業務フローシミュレーション 実行仕様書

**文書ID:** SIM-001
**バージョン:** 1.0
**配置先:** CRM リポジトリ `docs/BULK_SIMULATION_SPEC.md`
**対象システム:** CRM（AI_CRM）／ERP（Mini Global ERP）／AI Trade Management
**前提:** IF-01〜IF-32 の E2E 疎通テストが完了していること

---

# 0. Claude Code への実行指示

**この節は、本書を読んだ Claude Code が最初に実行する内容です。**

## 0.1 あなたのタスク

本書の仕様に従って、**3システムを横断する業務フローシミュレータを実装し、実行し、結果を検証してレポートする**こと。

## 0.2 進め方

以下のフェーズを**順番に**実施してください。各フェーズの完了条件を満たさずに次へ進まないこと。

| # | フェーズ | 完了条件 | 目安 |
|---|---|---|---|
| **P0** | 環境確認（§2） | 3システムが起動し、署名付きリクエストが往復する | 15分 |
| **P1** | 骨組みの実装（§3） | `run.py --dry-run` が全イベントを生成し、API を1本も叩かずに完走する | 60分 |
| **P2** | マスタ投入（§4） | 取引先25社・品目・許可証が3システムに登録され、突合が一致する | 45分 |
| **P3** | 小スモーク（§10.2） | `--scale=smoke`（商談3件）が完走し、検証が全項目パスする | 60分 |
| **P4** | 本実行（§5〜7） | 商談60件・12ヶ月分が完走し、`out/run_<id>/` が生成される | 45〜60分 |
| **P5** | 検証とレポート（§8〜9） | 検証結果と KPI レポートが出力され、逸脱が説明できる | 30分 |

## 0.3 厳守事項

1. **`--dry-run` を先に通すこと。** API を叩く前に、生成されるイベント列が仕様どおりかを必ず確認してください。実行してから間違いに気づくと、3システムのデータをクリーンアップする手間が発生します。
2. **期待値台帳（§7）を必ず実装すること。** シミュレータは「投入したもの」と「こうなっているはず」を自分で記録します。これがないと検証ができません。
3. **本番・ステージング環境では絶対に実行しないこと。** 実行前に接続先が検証環境であることを確認してください（§2.3 のガード）。
4. **中断・再開できるようにすること。** 60商談・12ヶ月の実行は数十分かかります。途中で失敗しても最初からやり直さずに済む設計にしてください（§11.2）。
5. **失敗を握りつぶさないこと。** API がエラーを返したら記録して継続し、最後にまとめて報告してください。例外を握りつぶして「成功」と報告するのが最悪の結果です。

## 0.4 このシミュレーションで答えを出したい問い

主目的は**業務フローの妥当性検証**です。「動くか」ではなく「**業務として回るか**」を見ます。実装中は常に以下を意識してください。

- 輸出管理担当の**審査待ちキューは何件まで積み上がるか**。人が捌ける量か
- **override は何件必要になるか**。承認者（部門長）の負荷は現実的か
- 審査鮮度切れによる**無駄な再審査が何件発生するか**
- 見積が `DRAFT` で**何日滞留するか**。営業が待たされる時間は許容範囲か
- ライセンス枠不足で**止まる契約が何件出るか**
- **誰も気づかないまま止まっている案件**が発生しないか

---

# 1. シミュレーションの目的と全体像

## 1.1 設定

| 項目 | 値 |
|---|---|
| 主目的 | 業務フローの妥当性検証 |
| 期間 | **2026-01-01 〜 2026-12-31（12ヶ月）を仮想カレンダーで圧縮進行** |
| 起点 | **CRM**（商談作成から開始） |
| 規模 | 取引先25社・商談60件・見積90件・契約35件・出荷69件 |
| AI判定を要する件数 | 約143件（並列5で約43分、並列10で約21分） |

## 1.2 商流の全体像

```
【CRM】取引先登録 ──IF-01──▶【AI_TM】スクリーニング（契約相手＋エンドユーザー）
   │                              │
   │◀─────────IF-11───────────────┘  結果・party_id
   ▼
【CRM】商談作成 → 見積 DRAFT
   ├──IF-03(provisional)──▶【AI_TM】仮審査（輸出ゲート）
   ├──IF-32───────────────▶【ERP】与信・反社チェック（商流ゲート）
   └──IF-06───────────────▶【AI_TM】ライセンス残枠照会
   ▼ 両ゲート通過
【CRM】見積 ISSUABLE → 顧客提出 → 契約書発行
   ├──IF-03(formal)──────▶【AI_TM】正式審査（仮審査を引き継ぎ）
   └──IF-07───────────────▶【AI_TM】ライセンス枠 仮引当
   ▼ 契約 close ＋ 商談 closing
【CRM】──IF-25──▶【ERP】受注転記（aitm_transaction_id 引き渡し・新規審査を起票しない）
   ▼
【ERP】出荷伝票 ──IF-20──▶【AI_TM】出荷前再スクリーニング
        │        ──IF-21──▶【AI_TM】ライセンス枠 消費確定
        ├──IF-29──▶【CRM】出荷実績
        └──IF-30──▶【CRM】請求実績（invoice）
   ▼
【CRM】closing 商談に売上実績が集計され、時系列分析が可能になる

【全期間】【AI_TM】継続監視 ──IF-16──▶【CRM】契約アラート
                          └──IF-24──▶【ERP】出荷HOLD
```

---

# 2. 前提条件と環境確認（P0）

## 2.1 3システムの起動確認

```bash
# CRM
curl -sf http://localhost:8000/health || echo "CRM 未起動"
# ERP
curl -sf http://localhost:8888/health || echo "ERP 未起動"
# AI_TM（各モジュール）
for p in 8000 8002 8003 8005 8011 8012; do
  curl -sf http://localhost:$p/health >/dev/null || echo "AI_TM :$p 未起動"
done
```

> ポート番号はご自身の環境に合わせて `config/endpoints.yaml` で設定してください。ハードコードしないこと。

## 2.2 接続設定

`config/endpoints.yaml` を作成します。

```yaml
crm:
  base_url: http://localhost:8000
  tenant_id: "<crm_tenant_id (UUID)>"
  bearer: ${CRM_BEARER}
  signing_secret: ${CRM_SIGNING_SECRET}
erp:
  base_url: http://localhost:8888
  client_id: "DEMO"
  bearer: ${ERP_BEARER}
  signing_secret: ${ERP_SIGNING_SECRET}
aitm:
  portal:         http://localhost:8000
  validation:     http://localhost:8011
  classification: http://localhost:8002
  screening:      http://localhost:8005
  rnd:            http://localhost:8003
  license:        http://localhost:8012
  org_id: "sim-org"
  bearer: ${AITM_BEARER}
  signing_secret: ${AITM_SIGNING_SECRET}
```

**署名の生成**（全リクエスト共通、E2E テストで実装済みのものを流用してください）:

```
署名対象 = "{timestamp}.{raw_body}"
X-Signature: sha256={HMAC-SHA256(secret, 署名対象).hexdigest()}
X-Timestamp: {Unix秒}
X-Request-Id: {UUIDv4}
X-Tenant-Id: {送信先システムの識別子}
Authorization: Bearer {token}
```

## 2.3 安全ガード（必須実装）

**本番・ステージングでの誤実行を防ぐガードを、実行の最初に入れてください。**

```python
def assert_safe_environment(cfg):
    allowed_hosts = {"localhost", "127.0.0.1", "host.docker.internal"}
    for name, url in cfg.all_base_urls():
        host = urlparse(url).hostname
        if host not in allowed_hosts and not os.getenv("SIM_ALLOW_REMOTE"):
            raise RuntimeError(
                f"{name} の接続先 {host} はローカルではありません。"
                f"検証環境であることを確認のうえ SIM_ALLOW_REMOTE=1 を設定してください。")
    # AI_TM のウォッチリストが本番リストでないことも確認
    if not cfg.aitm.get("test_watchlist_enabled"):
        raise RuntimeError("テスト用ウォッチリストが有効になっていません（§4.4）")
```

## 2.4 初期状態のクリーンアップ

シミュレーションは**クリーンな状態から開始**します。実行前に前回の実行データを削除してください。

```bash
python run.py --cleanup --run-id <前回のID>
```

クリーンアップ対象は、シミュレータが投入したデータのみ（`SIM-` プレフィックス付きのレコード）とします。**既存データを消さないよう、プレフィックスで必ず絞り込んでください。**

---

# 3. 実装アーキテクチャ（P1）

## 3.1 ディレクトリ構成

CRM リポジトリ配下に作成します。

```
simulation/
├── run.py                      # エントリポイント
├── config/
│   ├── endpoints.yaml          # 接続先（§2.2）
│   └── scenario.yaml           # 規模・分布パラメータ（§3.3）
├── src/
│   ├── clock.py                # 仮想時計（§3.4）
│   ├── rng.py                  # 乱数（seed 固定・再現性）
│   ├── clients/
│   │   ├── base.py             # 署名付き HTTP クライアント
│   │   ├── crm.py              # IF-01〜09, 25, 28, 32 の送信
│   │   ├── erp.py              # ERP 操作（出荷・請求・返品）
│   │   └── aitm.py             # AI_TM 操作（override・監視トリガ）
│   ├── generators/
│   │   ├── masters.py          # 取引先・品目・許可証（§4）
│   │   └── events.py           # 業務イベント生成（§5）
│   ├── flows/
│   │   ├── opportunity.py      # 商談〜見積
│   │   ├── contract.py         # 契約〜ERP転記
│   │   ├── fulfillment.py      # 出荷〜請求〜返品
│   │   └── exceptions.py       # 例外系（override・監視・失注）
│   ├── ledger.py               # 期待値台帳（§7）
│   └── verify/
│       ├── reconcile.py        # 3システム突合（§8.1）
│       ├── kpi.py              # 業務KPI算出（§8.2）
│       └── anomaly.py          # 異常検出（§8.3）
└── out/
    └── run_<YYYYMMDD-HHMMSS>/
        ├── events.jsonl        # 全イベント（時系列）
        ├── ledger.json         # 期待値台帳
        ├── api_log.jsonl       # 全API呼び出しの記録
        ├── findings.md         # 検出事項
        └── report.html         # KPI レポート
```

## 3.2 実行コマンド

```bash
python run.py --dry-run                 # API を叩かずイベント列だけ生成（P1）
python run.py --scale=smoke             # 商談3件のスモーク（P3）
python run.py --scale=medium            # 本実行：商談60件（P4）
python run.py --resume <run_id>         # 中断からの再開
python run.py --verify-only <run_id>    # 検証とレポートのみ再実行
python run.py --cleanup <run_id>        # 投入データの削除
```

## 3.3 `scenario.yaml`（パラメータの外出し）

**数値をコードに直書きしないでください。** チューニングして再実行するのがこのシミュレーションの主目的です。

```yaml
scale:
  medium:
    accounts: 25
    engagements: 60
    period: {start: "2026-01-01", end: "2026-12-31"}
  smoke:
    accounts: 3
    engagements: 3
    period: {start: "2026-01-01", end: "2026-03-31"}

seed: 20260814          # 乱数シード。固定して再現性を確保

quadrant_mix:           # §6.1
  existing_product_existing_customer: 0.60
  existing_product_new_customer:      0.25
  new_product_existing_customer:      0.10
  new_product_new_customer:           0.05

funnel:
  quote_reach_rate: 0.75        # 商談 → 見積作成に到達する率
  quote_revisions: {1: 0.40, 2: 0.35, 3: 0.17, 4: 0.08}
  win_rate: 0.58                # 商談 → 契約
  split_shipments: {1: 0.31, 2: 0.40, 3: 0.29}

lead_times_days:        # 平均値。実装は正規分布（σ=平均の30%、最小1日）
  engagement_to_quote: 20
  quote_revision_interval: 12
  quote_to_contract: 45         # ★審査鮮度30日を跨ぐため鮮度切れが自然発生する
  contract_to_first_shipment: 30
  shipment_interval: 60
  shipment_to_invoice: 5

exception_rates:        # §6.2
  party_match: 0.08             # 制裁ヒット
  party_possible_match: 0.12
  provisional_needs_review: 0.20
  override_success: 0.67        # needs_review のうち override で通過する割合
  formal_rejected_count: 1      # 件数指定
  license_shortage_count: 3
  monitoring_hit_count: 2
  return_count: 3
  deemed_export_count: 5
  unmapped_product_count: 2

execution:
  parallelism: 5                # AI判定の並列度
  judgment_poll_interval_sec: 5
  judgment_timeout_sec: 600
  api_retry: {max: 3, backoff_sec: [2, 8, 30]}
```

## 3.4 仮想時計

```python
class VirtualClock:
    """業務日付を進める。実時間とは切り離す。"""
    def __init__(self, start: date, end: date):
        self.today = start
        self.end = end

    def advance_to(self, d: date):
        assert d >= self.today, "時間を巻き戻してはいけない"
        self.today = min(d, self.end)

    def business_days_from(self, d: date, n: int) -> date:
        """土日を除いて n 営業日後"""
```

**時刻の扱いで重要な点**

| 項目 | 方針 |
|---|---|
| CRM/ERP に渡す業務日付 | **仮想時計の日付**（`document_date`, `contract_start_date` 等） |
| API リクエストの `X-Timestamp` | **実時刻**（署名検証は±300秒のため、仮想時刻を使うと必ず失敗する） |
| 審査鮮度（`valid_until`）の判定 | **仮想時計で評価する。** AI_TM が実時刻で判定する実装の場合は §11.3 の対処を参照 |

> **審査鮮度切れは本シミュレーションの重要な検証項目です。** AI_TM 側が `valid_until` を実時刻で判定していると、12ヶ月を数十分で走らせるこのシミュレーションでは鮮度切れが一度も発生しません。§11.3 の方法で必ず再現してください。

## 3.5 イベント駆動の実行モデル

```python
# 全イベントを事前生成 → 日付順にソート → 仮想時計を進めながら実行
events = generate_all_events(scenario, seed)   # dry-run はここまで
events.sort(key=lambda e: (e.date, e.seq))

for day, todays in groupby(events, key=lambda e: e.date):
    clock.advance_to(day)
    for ev in todays:
        result = execute(ev)          # API 呼び出し
        ledger.record(ev, result)     # 期待値台帳へ
        log_event(ev, result)
    process_pending_judgments()       # AI_TM の判定完了を回収
```

**イベントを事前生成する理由:** `--dry-run` で全体像を検証でき、再現性が確保でき、中断・再開が容易になります。

---

# 4. マスタデータ生成仕様（P2）

すべての識別子に **`SIM-` プレフィックス**を付け、クリーンアップ時に確実に絞り込めるようにしてください。

## 4.1 取引先 25社

| # | 分類 | 件数 | 属性 |
|---|---|---|---|
| 1 | ERP 既存・国内 | 8 | 日本。`erp_bp_code` あり。与信枠 3,000万〜1億円 |
| 2 | ERP 既存・海外 | 5 | KR/TW/US/DE/SG。`erp_bp_code` あり |
| 3 | **CRM 発生（ERP未登録）** | 7 | 引き合い時に CRM で新規作成。`erp_bp_code` なし |
| 4 | **商社（エンドユーザーが別）** | 3 | 日本の商社。エンドユーザーは別法人（CN/TW） |
| 5 | R&D 起点 | 2 | AI_TM の R&D 案件から発生 |

**エンドユーザーの設定（重要）**

- 上記 #4 の3社は、**契約相手とエンドユーザーが異なります**。エンドユーザーは中国・台湾の製造拠点とします
- それ以外は契約相手＝エンドユーザー（同一 party）ですが、**API には必ず `end_user_ref` を明示的に送ってください**
- **制裁ヒット2社のうち1社は、必ず「商社のエンドユーザー側」に配置してください。** 契約相手だけを見ていると見逃すケースの再現が、本シミュレーションの重要な検証項目です

**命名規則**

```
legal_name       : "SIM Trading {NN} Co., Ltd." 等（実在企業名を絶対に使わないこと）
crm_account_id   : CRM が採番
external_id      : ERP 既存のみ "SIM-BP-{NNNNNNN}"
```

## 4.2 品目 15品目

| ECCN | 件数 | 該非 | ライセンス | 用途 |
|---|---|---|---|---|
| EAR99 | 6 | NOT_APPLICABLE | 不要 | 通常フローの主流 |
| 3C001 | 4 | APPLICABLE | **必要** | ライセンス枠テスト |
| 3B001 | 2 | APPLICABLE | **必要** | 高リスク |
| 1C350 | 1 | APPLICABLE | 必要 | 化学兵器条約対象 |
| （未判定） | 2 | UNKNOWN | — | R&D 起点の新規品目 |

**加えて、`erp_material_code` を意図的に未設定にした品目を2件用意してください**（§6.2 の「品目マッピング未設定」の再現用）。CRM の `Product` には存在するが ERP 品目コードが紐付いていない状態です。

## 4.3 輸出許可証 4件

| 許可証 | 対象品目 | 仕向地 | 数量枠 | 意図 |
|---|---|---|---|---|
| SIM-LIC-01 | 3C001 | KR, TW | 5,000 | 通常の引当・消費 |
| SIM-LIC-02 | 3C001 | CN | **300** | **枠不足を発生させる（少なめに設定）** |
| SIM-LIC-03 | 3B001 | KR | 2,000 | 通常 |
| SIM-LIC-04 | 1C350 | TW | 1,000（**2026-09-30 失効**） | 契約期間内の失効警告 |

## 4.4 テスト用ウォッチリスト

**実制裁リストを使うため、必ず AI_TM のテスト専用ウォッチリストに架空企業を登録してください。** 実在の掲載企業名を使ってはいけません。

```
list_name: "SIM_TEST_WATCHLIST"
entries:
  - SIM Semiconductor Alpha Corp     # 即 match（§6.2 の2社のうち1社）
  - SIM Precision Beta Industries    # 即 match（商社のエンドユーザー側に配置）
  - SIM Materials Gamma Ltd          # possible_match
  - SIM Components Delta GmbH        # possible_match
  - SIM Systems Epsilon Pte          # possible_match
  - SIM Monitoring Zeta Corp         # 初期 clear → 期中に追加して継続監視を発火
```

**完了条件（P2）:** マスタ投入後、3システムで以下が一致すること。

- 取引先25社（＋エンドユーザー3社）が CRM に存在し、`aitm_party_id` が採番されている
- ERP 既存13社が ERP に存在し、`aitm_party_id` が CRM と一致する
- 品目15件（＋未マッピング2件）が CRM/ERP/AI_TM に存在する
- 許可証4件が AI_TM に登録され、残枠が初期値どおり

---

# 5. 業務イベント生成仕様（12ヶ月カレンダー）

## 5.1 商談の発生

```
2026-01 〜 2026-12 に商談60件を発生させる。
月次配分は均等ではなく、期末に山を作る（実業務を模す）:
  1月:4  2月:4  3月:7  4月:4  5月:4  6月:6
  7月:4  8月:3  9月:7  10月:4 11月:5 12月:8
（3月・9月・12月は四半期末／年度末で増加）
```

各商談に以下を割り当てます。

| 属性 | 決め方 |
|---|---|
| 象限 | §6.1 の配分に従う |
| 取引先 | 既存顧客なら既存25社から、新規顧客なら新規作成 |
| 品目 | 1〜3品目（1品目50%、2品目30%、3品目20%） |
| 金額 | 対数正規分布。中央値 500万円、範囲 50万〜1億円 |
| 通貨 | JPY 60%、USD 35%、EUR 5% |
| 仕向地 | JP 30%、KR 20%、TW 15%、CN 15%、US 10%、その他 10% |
| 営業担当 | 5名からランダム |

## 5.2 商談から見積へ

```
商談作成（clock.today）
  ↓ engagement_to_quote 日後（平均20日）
見積 DRAFT 作成
  ├─ IF-03 仮審査を起票（review_type=provisional）
  ├─ IF-32 与信・反社チェック
  └─ IF-06 ライセンス残枠照会（ライセンス必要品目のみ）
  ↓ 判定完了を待つ（最大 judgment_timeout_sec）
  ├─ 両ゲート通過 → 見積 ISSUABLE
  └─ 輸出ゲート needs_review → §6.2 の override フローへ
```

**見積の改訂**

`quote_revisions` の分布に従って改訂します。改訂の内容は以下から選びます。

| 改訂内容 | 割合 | `review_key_hash` |
|---|---|---|
| 備考・条件文のみ変更 | 30% | **不変**（再審査されないことを検証） |
| 値引き（同一金額帯内・3〜5%） | 25% | **不変** |
| 数量変更 | 25% | 変化 → 再審査 |
| 品目追加・削除 | 15% | 変化 → 再審査 |
| 仕向地変更 | 5% | 変化 → 再審査 |

> **`review_key_hash` が不変のとき AI_TM が再審査しないこと**は、輸出管理担当のキューを溢れさせないための中核機能です。改訂の55%が不変ケースになるよう配分しています。

## 5.3 見積から契約へ

```
見積 ISSUABLE
  ↓ quote_to_contract 日後（平均45日）※ここで審査鮮度30日を跨ぐケースが発生する
勝敗判定（win_rate = 0.58）
  ├─ 受注 → 契約書発行
  │    ├─ IF-03 正式審査（review_type=formal, parent_case_no=仮審査）
  │    │    ├─ 鮮度内かつハッシュ不変 → 引き継ぎで即完了
  │    │    └─ 鮮度切れ or ハッシュ変化 → フル再判定
  │    └─ IF-07 ライセンス枠 仮引当
  │         └─ 枠不足なら 409 → 契約書出力不可（§6.2）
  └─ 失注 → IF-08 審査取下げ ＋ ライセンス引当解放
```

## 5.4 契約から出荷・請求へ

```
契約 close ＋ 商談 closing
  ↓ IF-25 ERP へ受注転記（aitm_transaction_id を必ず引き渡す）
  ↓ contract_to_first_shipment 日後（平均30日）
出荷（split_shipments の分納回数だけ繰り返す。間隔は平均60日）
  ├─ IF-20 出荷前再スクリーニング
  ├─ IF-21 ライセンス枠 消費確定
  └─ IF-29 出荷実績を CRM へ
  ↓ shipment_to_invoice 日後（平均5日）
請求（invoice 発行）
  └─ IF-30 請求実績を CRM へ
```

> **12月に契約した案件は出荷が翌年になり、期末時点で受注残として残ります。これは正常です。**検証時に「出荷されていない契約がある」を異常として扱わないでください。

## 5.5 期中に発生させるイベント

| イベント | 発生タイミング | 内容 |
|---|---|---|
| 継続監視ヒット | 2026-07 と 2026-10 | ウォッチリストに `SIM Monitoring Zeta Corp` を追加 → **IF-12**（遡及再評価）／IF-16／IF-24 が発火 |
| みなし輸出 | 商談進行中にランダム5件 | IF-09 で技術資料共有イベントを送信 → CRITICAL は IF-13 で返る |
| 返品 | 出荷後30〜60日にランダム3件 | ERP で返品伝票 → IF-22／IF-31 |
| **ERP 単独受注** | 各四半期に1件（計4件） | **CRM を経由しない直販受注**を ERP に直接登録。**IF-17/18/19 の従来経路（ERP が自ら審査を起票する）が壊れていないことの回帰確認**。判定結果は IF-23 で ERP に返る |

---

# 6. シナリオ配分と例外系

## 6.1 4象限の配分（商談60件）

| 象限 | 件数 | 起点 | 特記 |
|---|---|---|---|
| 既存プロダクト × 既存顧客 | **36** | CRM | 基本フロー |
| 既存プロダクト × 新規顧客 | **15** | CRM | 取引先を CRM で新規作成（ERP 未登録）→ IF-01 |
| 新規プロダクト × 既存顧客 | **6** | **AI_TM（R&D）** | IF-14 で CRM に商談自動作成 → IF-26 で品目連動 |
| 新規プロダクト × 新規顧客 | **3** | **AI_TM（R&D）** | 同上＋取引先も新規 |

**R&D 起点の9件は `RND_INCUBATION` ステージで作成され、パイプライン集計から除外されること**を検証してください。品目が ERP 経由で CRM に到達した後に通常ステージへ昇格します。

## 6.2 例外系の発生設計

| 例外 | 件数 | 検証したいこと |
|---|---|---|
| 取引先 **match**（制裁ヒット） | 2社 | 商談・見積・契約の作成がブロックされる。**1社はエンドユーザー側に配置** |
| 取引先 possible_match | 3社 | 警告付きで進行でき、承認 ActionItem が起票される |
| 仮審査 **needs_review** | 18件 | 見積が DRAFT に固定され、顧客提出用 PDF が出力できない |
| └ **override** で通過 | 12件 | 承認者・理由・有効期限が記録され、CRM に表示される |
| └ 未解消のまま失注／保留 | 6件 | 滞留が可視化される |
| 正式審査 **rejected** | 1件 | 契約書が出力できず、法務確認待ちになる |
| **ライセンス枠不足** | 3件 | 見積時に警告が出て、契約発行時に引当が 409 になる |
| **契約期間中の制裁ヒット** | 2件 | IF-16 で CRM に、IF-24 で ERP に届き、未出荷分が HOLD される |
| 失注による審査取下げ | 25件 | 審査待ちキューから消え、ライセンス引当が解放される |
| 返品 | 3件 | 売上がマイナス計上され、ライセンス枠が戻し入れされる |
| みなし輸出イベント | 5件 | リスク判定され、CRITICAL は IF-13 で CRM に通知される |
| **品目マッピング未設定** | 2件 | **見積は作成できるが `UNKNOWN` でゲートがブロックされる**（`CLEAR` にフォールバックしないこと） |

> **最後の項目は特に重要です。** 判定不能を「問題なし」として扱う実装ミスは、制裁対象と知らずに商談が進む重大事故に直結します。フェイルクローズが機能しているかを必ず確認してください。

## 6.3 override の実行方法

override は AI_TM 側の操作です。シミュレータは AI_TM の override API を呼びます。

```python
aitm.apply_override(
    transaction_id=txn_id,
    scope="quote",
    reason_code="end_user_certificate_obtained",
    reason="顧客より最終用途誓約書および需要者証明書を取得。民生用途であることを確認済み。",
    approved_by={"name": "SIM 輸出管理部長", "title": "輸出管理部 部長"},
    approval_level="department_head",
    valid_until=clock.today + timedelta(days=90),
)
```

**検証ポイント:** 仮審査で override 通過した案件が契約発行に進んだとき、**override が正式審査に継承されず、フル再判定が走ること**。12件のうち少なくとも6件は契約まで進めて、この挙動を確認してください。

---

# 7. 期待値台帳（Ledger）

**シミュレータは「投入したもの」と「こうなっているはず」を自分で記録します。** これが検証の基準になります。

```python
@dataclass
class LedgerEntry:
    entity_type: str          # account | product | engagement | quote | contract | shipment | invoice | return
    sim_id: str               # シミュレータ内の一意ID
    created_on: date          # 仮想日付
    # 各システムでの実IDと状態
    crm: dict = field(default_factory=dict)   # {account_id, engagement_id, quote_id, contract_id, status}
    erp: dict = field(default_factory=dict)   # {bp_code, sales_order_number, delivery_number, billing_number}
    aitm: dict = field(default_factory=dict)  # {party_id, transaction_id, case_no, status, allocation_id}
    # 期待値
    expected: dict = field(default_factory=dict)
    # 実測
    actual: dict = field(default_factory=dict)
    anomalies: list = field(default_factory=list)
```

## 7.1 台帳に必ず記録する期待値

| 対象 | 期待値 |
|---|---|
| 契約 | 契約額、出荷予定総数量、分納回数、`aitm_transaction_id` |
| 出荷 | 数量、金額、消費されるライセンス枠 |
| 請求 | 金額 |
| ライセンス | 引当量、消費量、返品による戻し入れ量、最終残枠 |
| 審査 | 起票された審査の総数（**仮審査と正式審査の各1件のみで、重複がないこと**） |
| 例外 | 発生させたはずの例外の種別と件数 |

## 7.2 実行中の記録

すべての API 呼び出しを `api_log.jsonl` に記録してください。

```json
{"ts":"2026-03-14T02:11:33Z","virtual_date":"2026-03-14","sim_id":"ENG-0042",
 "interface":"IF-03","direction":"CRM->AI_TM","method":"POST",
 "url":".../api/transactions","request_id":"...","status":201,
 "latency_ms":842,"response_summary":{"transaction_id":"TXN-...","status":"in_review"}}
```

---

# 8. 検証仕様（P5）

## 8.1 3システム突合（reconcile）

| # | 照合内容 | 期待 |
|---|---|---|
| R-01 | `transaction_id` の一致 | 台帳 ＝ CRM 契約 ＝ ERP 受注 ＝ AI_TM 審査 |
| R-02 | `aitm_party_id` の一致 | 台帳 ＝ CRM Account ＝ ERP BP ＝ AI_TM party |
| R-03 | **審査案件の重複がないこと** | 1契約あたり 仮審査1件＋正式審査1件のみ。**ERP 転記で増えていない** |
| R-04 | 契約金額の一致 | CRM 契約額 ＝ ERP 受注額（為替換算後） |
| R-05 | 出荷実績の一致 | 台帳 ＝ CRM 出荷実績 ＝ ERP Delivery 合計 |
| R-06 | 請求実績の一致 | 台帳 ＝ CRM 請求実績 ＝ ERP 請求合計 |
| R-07 | 受注残の整合 | CRM 受注残 ＝ 契約額 − 出荷合計 |
| R-08 | **ライセンス枠の整合** | 初期枠 − 消費 − 引当残 ＝ AI_TM の現在残枠。返品分が戻し入れされている |
| R-09 | 失注案件の審査状態 | 25件すべてが `withdrawn`。引当が解放されている |
| R-10 | ERP 単独受注 | 4件が ERP 起票の審査（`link_source='erp'`）を持つ |

## 8.2 業務KPI（本シミュレーションの主目的）

**これが「業務として回るか」の答えです。** 以下を算出してレポートしてください。

### 輸出管理部門の負荷

| KPI | 算出 | 判断の目安 |
|---|---|---|
| **審査待ちキューの日次推移** | 各仮想日で `needs_review` かつ未対応の件数 | **ピークが10件を超えるなら要検討** |
| キューの最大滞留日数 | `needs_review` になってから解消までの最長日数 | 5営業日超は要検討 |
| **override 件数と承認レベル内訳** | 部門長承認が必要な件数 | 月2件超なら承認フローの見直し |
| override の理由内訳 | `reason_code` 別 | 同一理由が繰り返される＝ルール自体の見直し要 |
| 手動介入が必要だった総件数 | needs_review ＋ 枠不足 ＋ 監視ヒット | 部門の稼働可能工数と突合 |

### 営業の体感

| KPI | 算出 | 判断の目安 |
|---|---|---|
| **見積が DRAFT で滞留した日数** | 分布（中央値・p90・最大） | p90 が5営業日超なら商談速度に影響 |
| 顧客提出までのリードタイム | 商談作成 → 見積 ISSUABLE | — |
| **審査起因で失注した件数** | 未解消のまま失注した6件の内訳 | 機会損失額として金額も出す |
| 契約書が出せなかった件数 | rejected 1件 ＋ 枠不足3件 | — |

### 無駄・非効率の検出

| KPI | 算出 | 判断の目安 |
|---|---|---|
| **審査鮮度切れによる再審査回数** | フル再判定になった正式審査の件数 | **多いなら有効期限30日の見直しを検討** |
| ハッシュ不変で再利用された回数 | 節約できた審査件数 | 再利用が機能していない＝キューが溢れる |
| 判定完了までの所要時間 | 分布（中央値・p90） | — |
| リトライ・DLQ 滞留 | Outbox/Webhook の失敗件数 | **0であるべき** |

### 商流の健全性

| KPI | 算出 |
|---|---|
| 受注率 | 契約数 ÷ 商談数（期待値 58%） |
| 平均契約金額・売上実績合計 | — |
| 受注残（期末時点） | 契約額 − 出荷実績 |
| ライセンス枠の消費率 | 許可証ごとの消費／総枠 |

## 8.3 異常検出（anomaly）

**誰も気づかないまま止まっている案件**を検出します。これが業務妥当性検証の核心です。

| # | 検出内容 |
|---|---|
| A-01 | **どのシステムでも進行が止まり、かつアラート・ActionItem が起票されていない案件** |
| A-02 | 審査が完了しているのに CRM のゲートが更新されていない案件（Webhook 取りこぼし） |
| A-03 | CRM で契約 close 済みだが ERP に受注が存在しない案件 |
| A-04 | ERP で出荷済みだが CRM に実績が届いていない案件 |
| A-05 | ライセンスを消費したが対応する引当が存在しない出荷 |
| A-06 | 失注したのに審査が open のまま残っている案件 |
| A-07 | `UNKNOWN` が `CLEAR` に化けている案件（**フェイルクローズ違反。最重要**） |
| A-08 | 孤児レコード（親を持たない見積・契約・出荷） |

## 8.4 ネガティブ検証（期待した例外が期待どおり起きたか）

**例外が起きなかったこと自体が不具合**です。§6.2 の件数と実際の発生件数を突合してください。

```
期待: 制裁ヒットで商談作成ブロック 2社 → 実際: N社
期待: 仮審査 needs_review 18件         → 実際: N件
期待: ライセンス枠不足 3件             → 実際: N件
期待: 継続監視ヒット 2件               → 実際: N件
期待: 品目マッピング未設定で UNKNOWN 2件 → 実際: N件
```

差異があれば `findings.md` に理由とともに記載してください。

---

# 9. レポート仕様

`out/run_<id>/report.html` を単一ファイルの HTML として生成します（外部依存なし、インライン CSS/JS）。

| セクション | 内容 |
|---|---|
| 1. サマリ | 実行条件、総件数、突合結果（R-01〜R-10）の合否、異常検出件数 |
| 2. 商流ファネル | 商談 → 見積 → 契約 → 出荷 → 請求 の件数と金額の推移 |
| 3. **輸出管理負荷** | **審査待ちキューの日次推移グラフ**、override 内訳、手動介入件数 |
| 4. **営業の体感** | 見積 DRAFT 滞留日数の分布、リードタイム分布 |
| 5. 無駄・非効率 | 鮮度切れ再審査、再利用による節約、判定所要時間 |
| 6. ライセンス | 許可証ごとの枠消費推移、不足で止まった案件 |
| 7. 異常検出 | A-01〜A-08 の該当案件一覧（sim_id・システム別の状態つき） |
| 8. ネガティブ検証 | 期待 vs 実際の例外発生件数 |
| 9. 突合明細 | R-01〜R-10 の不一致明細 |

`findings.md` には、**業務上の示唆**を文章で記載してください。数値の羅列ではなく「審査待ちキューがピーク14件に達し、輸出管理担当1名では捌けない」といった判断材料の形にすること。

---

# 10. 実装フェーズと完了条件

## 10.1 P1: dry-run

```bash
python run.py --dry-run
```

**完了条件**

- API を1本も呼ばずに完走する
- 生成されたイベント数が §5・§6 の設計値と一致する（商談60・見積90・契約35・出荷69）
- イベントが日付順に並び、時間の巻き戻りがない
- 4象限の配分、例外系の件数が `scenario.yaml` どおり

**出力例**

```
[dry-run] period 2026-01-01 .. 2026-12-31
[dry-run] accounts=25(+3 end-users)  products=15(+2 unmapped)  licenses=4
[dry-run] engagements=60  quotes=90  contracts=35  shipments=69  invoices=69
[dry-run] exceptions: match=2 possible=3 needs_review=18 override=12
          rejected=1 license_short=3 monitoring=2 return=3 deemed_export=5 unmapped=2
[dry-run] AI judgments required: 143 (provisional 59 / formal-full 15 / shipment 69)
[dry-run] estimated wall time @parallelism=5: ~43 min
[dry-run] total events: 612
```

## 10.2 P3: スモーク

```bash
python run.py --scale=smoke
```

商談3件（各象限を1件以上含む）＋例外系1件で、**全 IF が最低1回ずつ通ること**を確認します。

**完了条件:** 突合 R-01〜R-10 が全項目パス、異常検出 A-01〜A-08 が0件。

## 10.3 P4: 本実行

```bash
python run.py --scale=medium
```

**完了条件:** 完走し、`out/run_<id>/` に4ファイルが生成される。API エラーは記録されていてよいが、**未説明のエラーが残っていないこと**。

---

# 11. 運用

## 11.1 冪等性

すべての投入は冪等にしてください。

| 対象 | 冪等キー |
|---|---|
| 取引先・品目 | `SIM-` プレフィックス付きの外部ID |
| 取引審査 | `case_no`（重複時は 409 が返るので既存 ID を採用する。**エラー扱いにしないこと**） |
| 受注転記 | `crm_contract_id`（重複時 409） |
| Webhook 受信 | `event_id` |

## 11.2 中断・再開

```bash
python run.py --resume <run_id>
```

`ledger.json` を毎日（仮想日付ごと）に保存し、再開時は最後に完了した仮想日付の翌日から続行します。

## 11.3 審査鮮度切れの再現（重要）

12ヶ月を数十分で走らせるため、AI_TM が `valid_until` を**実時刻**で評価していると鮮度切れが一度も発生しません。以下のいずれかで再現してください。

| 方法 | 内容 | 推奨 |
|---|---|---|
| **A** | AI_TM に `X-Sim-Now` ヘッダを追加し、検証環境でのみ仮想日付を基準に鮮度判定させる | **推奨。**最も正確 |
| B | `PROVISIONAL_REVIEW_VALID_DAYS` を極小（例 0）に設定した専用の実行を別途行う | 簡易だが全件が鮮度切れになる |
| C | CRM 側で仮想日付基準に鮮度を判定し、期限切れなら強制的にフル再判定を要求する | CRM のみで完結 |

**A を採用する場合、本番環境で `X-Sim-Now` が効かないようガードを入れてください。**

## 11.4 クリーンアップ

```bash
python run.py --cleanup <run_id>
```

`SIM-` プレフィックスのレコードのみを削除します。AI_TM のテスト用ウォッチリストのエントリも忘れずに削除してください。

---

# 12. トラブルシューティング

| 症状 | 確認 |
|---|---|
| 全リクエストが `401` | NTP 同期。`X-Timestamp` に**実時刻**を使っているか（仮想時刻ではない） |
| 署名が通らない | 署名対象と送信ボディが**同一バイト列**か。`json=` ではなく `content=` を使う |
| Webhook が届かない | 送信側の Outbox/DLQ を確認。`X-Tenant-Id` が送信先の識別子になっているか |
| 判定が完了しない | AI_TM の判定キューを確認。`judgment_timeout_sec` を延長 |
| 審査件数が想定より多い | `review_key_hash` の生成が改訂ごとに変わっていないか（§5.2） |
| **審査件数が契約数の2倍** | **IF-25 で `aitm_transaction_id` が渡っていない。**ERP が新規審査を起票している |
| 鮮度切れが発生しない | §11.3 |
| ライセンス残枠が合わない | 引当の解放漏れ（失注時）、返品の戻し入れ漏れ |
| 実行が極端に遅い | `parallelism` を上げる。ただし AI_TM の判定並列度が上限 |

---

# 付録A. 主要インターフェース早見表

| ID | 方向 | 用途 | 呼ぶタイミング |
|---|---|---|---|
| IF-01 | CRM→AI_TM | 取引先スクリーニング | 取引先登録時 |
| IF-03 | CRM→AI_TM | 取引審査（provisional/formal） | 見積作成時／契約発行時 |
| IF-06 | CRM→AI_TM | ライセンス残枠照会 | 見積作成時 |
| IF-07 | CRM→AI_TM | ライセンス枠 仮引当／解放 | 契約発行時／失注時 |
| IF-08 | CRM→AI_TM | 審査の取下げ | 失注・契約キャンセル時 |
| IF-09 | CRM→AI_TM | みなし輸出イベント | 技術資料共有の記録時 |
| IF-10 | AI_TM→CRM | 判定結果通知 | 審査完了時（受信） |
| IF-11 | AI_TM→CRM | スクリーニングアラート／名寄せ | （受信） |
| IF-12 | AI_TM→CRM | 制裁リスト更新の遡及再評価 | 期中2件（受信） |
| IF-13 | AI_TM→CRM | みなし輸出リスク通知 | CRITICAL 判定時（受信） |
| IF-14 | AI_TM→CRM | R&D案件からの商談作成依頼 | R&D起点9件（受信） |
| IF-15 | AI_TM→CRM | override 適用通知 | override 実行時（受信） |
| IF-16 | AI_TM→CRM | 継続監視アラート | 期中2件（受信） |
| IF-17 | ERP→AI_TM | 取引先の自動スクリーニング | ERP単独受注4件の取引先登録時 |
| IF-18 | ERP→AI_TM | 品目の自動分類 | ERP での品目登録時 |
| IF-19 | ERP→AI_TM | 受注の取引審査（ERP単独） | **ERP単独受注4件（回帰確認）** |
| IF-20 | ERP→AI_TM | 出荷前再スクリーニング | 出荷時 |
| IF-21 | ERP→AI_TM | ライセンス枠 消費確定 | 出荷計上時 |
| IF-22 | ERP→AI_TM | 返品・再輸出の審査 | 返品時 |
| IF-23 | AI_TM→ERP | 判定結果通知 | 審査完了時（ERP が受信） |
| IF-24 | AI_TM→ERP | 出荷HOLD／解除 | 監視ヒット時（受信） |
| IF-25 | CRM→ERP | 契約close時の受注転記 | 契約close時 |
| IF-26 | ERP→CRM | 品目マスタ連携 | R&D起点の品目登録時（受信） |
| IF-29 | ERP→CRM | 出荷実績 | 出荷計上時（受信） |
| IF-30 | ERP→CRM | 請求実績 | invoice発行時（受信） |
| IF-31 | ERP→CRM | 返品実績 | 返品計上時（受信） |
| IF-32 | CRM→ERP | 与信・反社チェック | 見積DRAFT作成時 |

---

**改訂履歴**

| 版 | 日付 | 内容 |
|---|---|---|
| 1.0 | 2026-08-14 | 初版。中規模（商談60件）・12ヶ月圧縮・業務妥当性検証 |
