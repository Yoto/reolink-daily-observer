# Configuration

設定は主に `.env`、`config/config.yaml`、private な `config/scene.yaml` の3か所に分かれています。

- `.env`: ホスト側のパス、API key、Docker 実行時の上書き
- `config/config.yaml`: 解析処理の既定値
- `config/scene.yaml`: 撮影場所、世帯、定型行動などの private な文脈

日々の実行方法は [operations.md](operations.md)、判定精度の改善方法は [tuning-and-evaluation.md](tuning-and-evaluation.md) を参照してください。

## Environment variables

`.env.example` を `.env` へコピーして編集します。

```dotenv
CAMERA_INPUT_DIR=C:/reolink
ANALYSIS_OUTPUT_DIR=C:/reolink-analysis/output
ANALYSIS_STATE_DIR=C:/reolink-analysis/state
ANALYZER_UID=10001
ANALYZER_GID=10001

GENAI_PROVIDER=openai
GENAI_MODEL=gpt-5.6-luna
OPENAI_API_KEY=replace-with-your-openai-api-key
GENAI_BATCH_ENABLED=true
ANALYZER_TMPFS_SIZE=4g
LOG_LEVEL=INFO
```

Windows のパスは `/` 区切りを推奨します。Docker Desktop では `ANALYZER_UID` / `ANALYZER_GID` の既定値をそのまま使えます。Linux へ移行する場合は専用実行ユーザーの `id -u` / `id -g` に合わせます。

API key はチャット、ソースコード、YAML、PowerShell のコマンド引数へ書かず、ローカルの `.env` だけに保存してください。`.env` と `.env.*` は Git と Docker build context の除外対象です。

## config.yaml

`config/config.yaml` が解析処理の既定値です。ここでは主な設定だけを説明します。正確な既定値はファイル本体を参照してください。

### Input

```yaml
input:
  stable_seconds: 120
  stability_recheck_sec: 2
  ffprobe_timeout_sec: 60
  extensions: [.mp4]
```

入力 MP4 は一度一覧化した後、size と mtime が変化しないことを再確認してから処理します。FTP 転送中のファイルを誤って解析しないための待機です。

入力は次のどちらの日付レイアウトにも対応します。

```text
C:\reolink\2026\08\16\*.mp4
C:\reolink\2026-08-16\*.mp4
```

### Frame extraction

```yaml
frames:
  interval_sec: 1
  max_long_edge_px: 1280
  jpeg_quality: 85
  ffmpeg_timeout_sec: 900
```

通常は1秒間隔で JPEG を抽出します。長い動画はリクエスト上限に合わせて時系列 chunk へ分割されます。

### Daytime resolution reduction

```yaml
frames:
  resolution_reduction:
    enabled: true
    daytime_max_long_edge_px: 768
    sample_frames: 5
    interval_sec: 1
    sample_width: 160
    sample_height: 90
    saturation_threshold: 20
    dark_luminance_threshold: 40
    dark_ratio_threshold: 0.20
```

解像度の選択は動画単体で完結し、録画時刻、緯度経度、前の動画の状態は使用しません。

動画先頭から1秒間隔で最大5フレームを160×90へ縮小し、彩度中央値と、輝度40未満の画素が占める暗部率を測定します。全フレームを集約した彩度中央値が20を超え、かつ暗部率が20%未満の場合だけ明らかな昼間とみなし、解析用フレームの長辺を768pxにします。

境界値を含む判定不能な動画、夜間候補、映像デコーダーの警告、測定失敗は安全側の1280pxを使用します。`enabled: false` にすると常に `frames.max_long_edge_px` を使用します。

### GenAI

```yaml
genai:
  provider: openai
  model: gpt-5.6-luna
  max_inline_image_bytes: 13000000
  chunk_overlap_frames: 2
  request_timeout_sec: 180
  max_output_tokens: 32768
  retry:
    max_attempts: 4
```

model ID、利用可否、料金は OpenAI 側で変更され得ます。料金見積りは `config/config.yaml` の単価による参考値で、請求画面が正です。

### Batch

```yaml
genai:
  batch:
    enabled: true
    completion_window: 24h
    poll_interval_sec: 30
    max_wait_sec: 90000
    max_requests_per_batch: 50000
    max_input_bytes: 180000000
    discount_ratio: 0.5
    delete_input_file: true
```

`enabled: false`、環境変数 `GENAI_BATCH_ENABLED=false`、実行時の `--sync` のいずれでも同期実行に戻せます。`mock` provider は Batch API に対応しないため常に同期実行です。

`discount_ratio` はローカルの料金見積りだけに使用し、送信内容や実際の請求には影響しません。

運用上の制約は [operations.md](operations.md#batch-api) を参照してください。

### Triage

```yaml
triage:
  enabled: true
  attention_score_threshold: 7
  notable_always_attention: true
  routine_explained_score_cap: 4
  group_related_events: true
  history_days: 14
  max_attention_items: 20
```

`attention_score_threshold` 以上の event に加え、`notable_always_attention` が true の場合は notable が記載された event も要確認へ抽出されます。

`routine_explained_score_cap` は、routine_explanation が記入され、かつ notable が空の event に対して anomaly_score の上限をローカルで強制します。既定の4は attention 閾値7を下回るため、scene で説明できる定型行動を抑制できます。notable がある場合には上限を適用しません。

`group_related_events` は、来客の到着から見送りまでのように同じ出来事が複数の録画へ分割されたケースを日報上で1件にまとめます。

`history_days` は普段との差分を判断するために triage へ渡す過去の日報の日数です。過去 event JSON 全体ではなく、各日の概要と傾向、要注意記載だけを渡します。

`enabled: false` にすると triage を実行せず、要確認セクションのない日報を生成します。

## scene.yaml

`config/scene.yaml` は、このカメラが普段何を映しているかを記述する private なファイルです。世帯構成や生活パターンを含むため Git にも Docker image にも入りません。

`config/scene.example.yaml` をコピーして編集してください。全項目が省略可能で、未設定の項目は prompt から丸ごと省かれます。ファイル自体が無い場合は警告を出して scene なしで動作します。

| 項目 | 内容 |
| --- | --- |
| `location` | 敷地の種別と周辺環境 |
| `camera_view` | 画角に入る範囲。PTZ で巡回する先も含める |
| `household` | 世帯構成と普段の行動。役割で書き、個人名は不要 |
| `routine_patterns` | 定期的に発生し、通常は指摘不要な行動 |
| `known_vehicles` | 自宅の車両 |
| `expected_visitors` | 配達や収集など想定内の来訪 |
| `notes` | 死角、画角に入る隣家、季節要因など |

外見ではなく振る舞いを書いてください。「白い車」より「平日朝7時台から8時台に自転車で出入口へ向かう」のような記述の方が routine の照合に有効です。

`expected_visitors` には、お盆や正月など季節によって正常になる来客も記述できます。triage は対象日の日付と突き合わせて判断します。

別の場所へ置く場合は `ANALYZER_SCENE_FILE` で上書きできます。

```powershell
docker compose --env-file .env run --rm -e ANALYZER_SCENE_FILE=/config/scene.local.yaml analyzer
```

scene 改善用の補助コマンドについては [scene-author.md](scene-author.md) を参照してください。

## Privacy

- MP4 ファイル名、event JSON、daily report、SQLite、ログには在宅状況などが表れる可能性があります。
- 出力・state・log ディレクトリは本人または専用実行ユーザーだけが読める場所に置いてください。
- API へ送信するのは observation では縮小 JPEG と prompt、triage と daily report では event JSON とテキストです。
- scene の内容は triage prompt に含まれて API へ送信されます。送信して差し支えない粒度で記述してください。
- `.gitignore` / `.dockerignore` による scene の保護は `git add -f` や誤ったイメージ公開まで防ぐものではありません。
