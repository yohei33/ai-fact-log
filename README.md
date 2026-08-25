# ai-fact-log

## What is this?

`ai-fact-log` は、AIに関する一次情報ベースのFactを日次・時系列で保存する共有ログです。人間がそのまま読め、特定のAIサービスに依存せず、将来はプログラムからPostgreSQLなどへ取り込める形を保ちます。

## What this is NOT

このRepositoryは、次のものではありません。

- AIニュースメディア
- 個人ブログ
- 未来予測Repository
- AKARI本体
- Personal Brain Lab本体
- 投資Recommendation
- AIによる意見集

個人向けの解釈、影響分析、予測、感情、推奨はFactに混ぜません。

## Data principles

- **Primary source first** — 可能な限り公式発表、公式文書、論文などの一次情報を使う
- **Timestamped** — 情報源の公開日時と収集日時を区別する
- **Traceable source** — 各Factから確認元URLを追跡できるようにする
- **Minimal interpretation** — 情報源から確認できる事実を簡潔に記録する
- **Human-readable** — 日次Markdownをそのまま読んだりAIへ渡したりできる
- **Machine-readable** — 固定SchemaのYAMLブロックとJSONL Indexを使う
- **Git history preserved** — Source Factの修正履歴をGitに残す

## Directory structure

```text
.
├── README.md
├── daily/
│   └── YYYY/MM/YYYY-MM-DD.md  # 1日分のFact（US、CN、JP、その他の順）
├── schema/
│   └── fact-schema.md         # フィールド、値、検証規則
├── templates/
│   └── daily-template.md      # 新しい日次ファイルの雛形
└── index/
    └── events_index.jsonl     # 重複確認・検索用の軽量Index
```

日次MarkdownがFactの正本です。`events_index.jsonl` は検索と重複排除のための派生Indexであり、Fact本文を重複して保持しません。

## How to use

1. 必要な `daily/YYYY/MM/YYYY-MM-DD.md` を取得します。
2. そのまま読むか、GPT、Claudeなどへアップロードします。
3. 自分の関心に合わせて、特定のFactの要約、比較、追加確認などを質問します。

新しいFactを追加する場合は、[Schema](schema/fact-schema.md)に従い、[Template](templates/daily-template.md)をコピーします。同じ変更で `index/events_index.jsonl` に対応する1行を追加し、Repository全体で `event_id` と `source_url` の重複を確認します。正式な日次運用では原則として `VERIFIED_PRIMARY` または `VERIFIED_PRIMARY_ARCHIVED` のFactだけを採用します。

## Fact Layer boundary

このRepositoryは共有Fact Layerだけを担います。AKARI固有のObservationや個人向け分析は下流で生成します。

```text
ai-fact-log (Shared Fact)
          ↓
      PostgreSQL
          ↓
 Personal Brain Lab
          ↓
  AKARI Observation
```

この境界により、同じFactを人間、複数のAI、将来のローカルAIやImporterが、それぞれ独立して再利用できます。
