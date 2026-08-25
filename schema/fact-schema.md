# Fact Schema

## Canonical record format

Factの正本は `daily/YYYY/MM/YYYY-MM-DD.md` に置く、1件につき1つの fenced YAML block です。地域見出しとFact見出しは閲覧用であり、ImporterはYAML block内のフィールドだけを正規データとして扱います。

必須キーは次の順序で記述します。順序は可読性のために固定しますが、Importerはキー名で処理してください。

```yaml
event_id: organization-slug-YYYYMMDD-NNN
title: Factを識別できる簡潔な表題
fact: >-
  情報源で直接確認できる事実。解釈、予測、推奨を含めない。
organization: 組織名
region: US
category: Product
published_at: "2026-08-25T09:00:00Z"
captured_at: "2026-08-25T10:30:00Z"
source_type: official_announcement
source_url: "https://example.invalid/source"
verification: VERIFIED_PRIMARY
```

すべてのキーを必ず持たせます。値が不明な `published_at` だけは推測せず `null` とします。空文字や `unknown` は使いません。

## Field definitions

| Field | Type | Rules |
|---|---|---|
| `event_id` | string | Repository全体で一意かつ不変。下記のID規則に従う |
| `title` | string | Factの内容を中立的かつ簡潔に表す |
| `fact` | string | Sourceから直接確認できる最小限の事実。Observationを含めない |
| `organization` | string | 発表または公開主体の正式名称 |
| `region` | enum | `US`, `CN`, `JP`, `GLOBAL`, `OTHER` |
| `category` | string | 安定した分類名。推奨値は下記参照 |
| `published_at` | string or null | Source側の公開日時。ISO 8601。判明している精度だけを記録 |
| `captured_at` | string | Factを確認した日時。タイムゾーン付きISO 8601 |
| `source_type` | enum | 下記の許容値を使用 |
| `source_url` | string | 直接確認したSourceの絶対HTTPS URL |
| `verification` | enum | 下記の許容値を使用 |

### `event_id`

形式は `<organization-slug>-<YYYYMMDD>-<NNN>` とします。

- `organization-slug`: 小文字ASCII英数字とハイフン。公開主体を安定して表す（例: `openai`, `anthropic`, `google-deepmind`）
- `YYYYMMDD`: 原則として `published_at` の暦日。公開日不明の場合は収集日
- `NNN`: 同じ主体・日付内で `001` から採番
- 正式採用後は、日次ファイルを移動・訂正してもIDを変更しない
- 削除済みIDを別のFactに再利用しない

手動採番時は、追加前に `events_index.jsonl` とRepository全体を検索します。将来のImporterでは `event_id` にUNIQUE制約を設定します。

### `published_at` and `captured_at`

- 日時が分かる場合: `"YYYY-MM-DDTHH:MM:SSZ"` またはUTC offset付き文字列
- 公開日だけ分かる場合: `"YYYY-MM-DD"`
- 公開日時が不明: `null`
- `captured_at` は常に日時とタイムゾーンを含める
- YAMLによる暗黙の日付型変換を避けるため、日時文字列は引用符で囲む

### `category`

初期の推奨値は `Model`, `Product`, `Research`, `Safety`, `Policy`, `Infrastructure`, `Business`, `Other` です。新しい値は必要性を確認して追加できますが、表記揺れを避け、既存値を優先します。

### `source_type`

許容値:

- `official_announcement`
- `official_blog`
- `official_documentation`
- `official_github`
- `paper`
- `government`
- `primary_other`
- `secondary`

### `verification`

許容値:

- `VERIFIED_PRIMARY`: 一次Sourceを直接確認済み
- `VERIFIED_PRIMARY_ARCHIVED`: 一次Sourceと、その保存版を確認済み
- `SECONDARY`: 二次Sourceに基づく
- `UNVERIFIED`: 未検証、またはSchema確認用サンプル

正式Factは原則として先頭2値だけを使用します。

## Daily file rules

- パスの日付は原則として `captured_at` の暦日と一致させる
- 見出しは `US`, `CN`, `JP`, `GLOBAL`, `OTHER` の順とする
- 該当Factがない地域も見出しを残し、`_No facts recorded._` と記す
- Fact見出しは閲覧用に `### Organization — Title` とする
- 各FactのYAML blockには、必須キー以外を勝手に追加しない。Schema拡張はこの文書を先に更新する
- Sourceの誤認や誤記を直す場合は、元のGit履歴を保った通常のcommitで修正する

## JSONL index contract

`index/events_index.jsonl` はUTF-8のJSON Lines形式で、空行を入れず、1 Factにつき1 JSON objectを1行に記録します。

```json
{"event_id":"organization-slug-YYYYMMDD-NNN","date":"2026-08-25","organization":"Organization","source_url":"https://example.invalid/source","verification":"VERIFIED_PRIMARY"}
```

`date` は対応する日次ファイルの日付です。Indexは `event_id`, `date`, `organization`, `source_url`, `verification` だけを持ち、表題やFact本文は日次Markdownから取得します。

## Importer invariants

将来のImporterは、次を検証してからINSERTしてください。

1. YAML blockに必須キーが過不足なく存在する。
2. enum、ID、日時、URLの形式が有効である。
3. 日次MarkdownとJSONLで `event_id` が一致する。
4. Repository内およびPostgreSQL内で `event_id` が一意である。
5. 再実行時は既登録IDをskipし、Fact本文の自動上書きをしない。

Markdown見出しや自然文からフィールドを推測してはいけません。
