# Development

この文書はローカル開発、テスト、mock provider、Docker image の更新ルールをまとめます。

システム構成は [architecture.md](architecture.md)、判定ロジックの回帰評価は [tuning-and-evaluation.md](tuning-and-evaluation.md) を参照してください。

## Local development

Python の依存関係を同期し、テストを実行します。

```bash
uv sync
uv run pytest -W error
```

通常の unit / integration test は API を呼びません。実 API key をテスト環境へ渡す必要はありません。

## Mock provider

Docker、ffprobe、ffmpeg、state 管理、出力生成までの配管を、実 API を呼ばずに E2E 確認できます。

`.env` の次の2行を一時的に変更します。

```dotenv
GENAI_PROVIDER=mock
GENAI_MODEL=mock-observer-v1
```

その後、通常と同じコマンドを実行します。

```bash
docker compose --env-file .env run --rm analyzer --date 2026-08-16
```

mock provider は映像内容を解析しません。生成された event や日報を判定品質の評価には使用しないでください。

mock provider は Batch API に対応しないため、設定に関わらず同期実行になります。

## Docker rebuild rules

`prompts/` と `templates/` は Dockerfile の `COPY` で image に入りますが、docker compose では作業ツリーを read-only mount して上書きします。

そのため通常の compose 開発では:

- `prompts/` の変更: 再ビルド不要
- `templates/` の変更: 再ビルド不要
- `app/` 以下のコード変更: 再ビルドが必要
- Dockerfile / dependency 変更: 再ビルドが必要

コード変更後:

```bash
docker compose --env-file .env build
```

## Prompt and scene changes

triage prompt や scene を変更するときは、単発の目視確認だけでなく triage regression fixture を使用してください。

```bash
docker compose run --rm analyzer triage-eval run
```

誤判定の再現ケースを追加してから修正する手順は [tuning-and-evaluation.md](tuning-and-evaluation.md) にあります。

実際の複数動画から scene へ追加する候補文を作る場合は [scene-author.md](scene-author.md) を使用します。

## Single-video prompt evaluation

1本の動画だけを同期解析できます。

```bash
docker compose --env-file .env run --rm analyzer analyze-video '/data/input/YYYY/MM/DD/Security camera_00_YYYYMMDDhhmmss.mp4'
```

`analyze-video` は常に同期実行なので、event observation prompt の変更を素早く確認する用途に向いています。

## Privacy in development

このリポジトリのテスト対象には家庭内の実データが含まれる場合があります。

- 実 MP4、event JSON、daily report、state DB、ログを repository へ commit しない
- `config/scene.yaml` を commit しない
- triage-eval fixture は公開 repository ではなく `/data/state` に置く
- scene-author の `--json-output` も private な state 領域へ保存する
- 実 API key は `.env` 以外へ書かない

fixture やログには映像そのものがなくても生活パターンが含まれるため、通常のテストデータと同じ感覚で公開しないでください。
