# Reolink Daily Observer PoC

Reolink RLC-823S1 が FTP 転送した MP4 を日単位で観察し、1動画ごとの event JSON と、その日の `daily_report.json` / `.md` / `.html` を生成する PoC です。映像から直接観察できる出来事を客観的・時系列的に記録し、危険性・犯罪可能性・異常度などの判定は行いません。

既定の実行環境は Windows + Docker Desktop の Linux コンテナです。入力 MP4 は read-only で mount し、移動・変更・削除しません。

## 処理の流れ

1. 対象日の MP4 を列挙し、転送が完了していることを確認します。
2. ffprobe で duration / resolution / fps / codec を取得します。
3. ffmpeg で1秒間隔、長辺1280px、JPEG quality 85のフレームを一時抽出します。
4. OpenAI の画像入力と Structured Outputs で、1 MP4 = 1 event JSON を生成します。
5. event JSON だけを入力にして日報を生成し、JSON / Markdown / HTML に描画します。
6. fingerprint、処理結果、API usage を SQLite に記録し、変更のない再実行をcacheします。

長い動画はフレームを時系列chunkに分割し、chunk結果を最後に1 eventへ統合します。一時JPEGはコンテナの `/tmp` に置き、処理後に削除します。

`GENAI_PROVIDER=mock` は Docker、ffprobe、ffmpeg、状態管理、出力までの配管確認専用です。画像内容は解析しません。

## セットアップ

前提:

- Docker Desktop が起動し、Linux containers が利用できること
- `docker compose version` が成功すること
- OpenAI APIを使う場合は、専用Projectで発行したAPI keyがあること

プロジェクトルートで次を実行します。

```powershell
Copy-Item -LiteralPath .env.example -Destination .env
New-Item -ItemType Directory -Force -Path C:\reolink-analysis\output
New-Item -ItemType Directory -Force -Path C:\reolink-analysis\state
notepad .env
```

`.env` の例:

```dotenv
CAMERA_INPUT_DIR=C:/reolink
ANALYSIS_OUTPUT_DIR=C:/reolink-analysis/output
ANALYSIS_STATE_DIR=C:/reolink-analysis/state
ANALYZER_UID=10001
ANALYZER_GID=10001

GENAI_PROVIDER=openai
GENAI_MODEL=gpt-5.6-luna
OPENAI_API_KEY=replace-with-your-openai-api-key
LOG_LEVEL=INFO
```

API key はチャット、ソースコード、YAML、PowerShellのコマンド引数へ書かず、ローカルの `.env` だけに保存してください。`.env` と `.env.*` は Git と Docker build context の除外対象です。PoC専用のOpenAI Projectとkeyを使い、Project側で予算上限・usage alertを設定し、終了後にkeyをrevokeする運用を推奨します。

入力は次のどちらの日付レイアウトにも対応します。

```text
C:\reolink\2026\08\16\*.mp4
C:\reolink\2026-08-16\*.mp4
```

## 実行

イメージを構築します。

```powershell
docker compose --env-file .env build
```

前日分を処理:

```powershell
docker compose --env-file .env run --rm analyzer
```

日付指定:

```powershell
docker compose --env-file .env run --rm analyzer --date 2026-08-16
```

単一動画:

```powershell
docker compose --env-file .env run --rm analyzer analyze-video '/data/input/YYYY/MM/DD/Security camera_00_YYYYMMDDhhmmss.mp4'
```

cacheを無視して再解析する場合だけ `--force` を追加します。

出力例:

```text
C:\reolink-analysis\output\2026-08-16\
├─ events\event_2026-08-16_<hash>.json
├─ daily_report.json
├─ daily_report.md
└─ daily_report.html

C:\reolink-analysis\state\state.sqlite
```

終了コードは `0` が成功、`1` が設定・日報などの致命的失敗、`2` が一部動画の失敗または保留です。

## mockによるE2E確認

実APIを呼ばずに配管を確認できます。`.env` の2行だけ一時的に変更します。

```dotenv
GENAI_PROVIDER=mock
GENAI_MODEL=mock-observer-v1
```

その後、通常と同じ `docker compose ... run --rm analyzer --date ...` を実行します。mock出力は映像内容の評価には使用できません。

## 主な設定

`config/config.yaml`:

```yaml
timezone: Asia/Tokyo
input:
  stable_seconds: 120
  stability_recheck_sec: 2
  ffprobe_timeout_sec: 60
frames:
  interval_sec: 1
  max_long_edge_px: 1280
  jpeg_quality: 85
  ffmpeg_timeout_sec: 900
genai:
  provider: openai
  model: gpt-5.6-luna
  max_inline_image_bytes: 13000000
  chunk_overlap_frames: 2
  request_timeout_sec: 180
  max_output_tokens: 8192
processing:
  continue_on_error: true
```

`gpt-5.6-luna` はコストを抑えた既定値です。model ID、利用可否、料金はOpenAI側で変更され得るため、実行前に契約Projectで確認してください。料金見積りは `config/config.yaml` の単価による参考値で、請求画面が正です。

## 定期実行

Windows Task Schedulerでは、04:00に次を起動します。

```text
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\path\to\reolink\scripts\run_daily.ps1" -ProjectPath "C:\path\to\reolink" -LogPath "C:\reolink-analysis\daily.log"
```

wrapperは `docker compose run --rm analyzer` を1回実行し、その終了コードを返します。特定日のbackfillには `-Date 2026-08-16`、明示的な再解析には `-Force` を追加します。

Linux移行用の例は `deploy/reolink-analyzer.service`、`.timer`、`crontab.example` にあります。systemd timerとcronはどちらか一方だけを使用してください。

## 開発・テスト

```powershell
uv sync
uv run pytest -W error
```

テストはAPIを呼びません。実keyをテスト環境へ渡す必要はありません。

## プライバシー

- MP4ファイル名、event JSON、日報、SQLite、ログには在宅状況などが表れる可能性があります。
- 出力・state・logディレクトリは本人または専用実行ユーザーだけが読める場所に置いてください。
- HTMLはstandaloneですが、信頼できない相手へ公開しないでください。
- APIへ送信するのは縮小JPEGとpromptです。日報段階ではevent JSONだけを送信します。
