# Reolink Daily Observer PoC

Reolink RLC-823S1 が FTP 転送した MP4 を日単位で観察し、1動画ごとの event JSON と、その日の `daily_report.json` / `.md` / `.html` を生成する PoC です。

観察と判定は分離しています。event JSON は映像から直接確認できる事実だけの客観的な記録で、危険性や犯罪可能性の判定を含みません。そのうえで triage 段が event JSON をテキストとして読み、住人が実際に確認すべきイベントだけを抽出します。日報は要確認イベントを先頭に置き、該当がなければその旨を明示します。

triage は画像を扱わないため、撮影場所の説明や抽出閾値を変更しても、動画を再解析せずに判定だけをやり直せます。

既定の実行環境は Windows + Docker Desktop の Linux コンテナです。入力 MP4 は read-only で mount し、移動・変更・削除しません。

## 処理の流れ

1. 対象日の MP4 を列挙し、転送が完了していることを確認します。
2. ffprobe で duration / resolution / fps / codec を取得します。
3. ffmpeg で1秒間隔、長辺1280px、JPEG quality 85のフレームを一時抽出します。
4. OpenAI の画像入力と Structured Outputs で、1 MP4 = 1 event JSON を生成します。
5. event JSON と撮影場所の説明、直近の日報履歴を入力に triage を1回実行し、全イベントへ `assessment` / `person_type` / `notable` / `anomaly_score` を付与します。
6. 閾値以上のスコア、または `notable` が記載されたイベントを要確認として抽出します（抽出はコード側で機械的に行い、モデルの選別に委ねません）。
7. event JSON と triage 結果を入力に日報を生成し、JSON / Markdown / HTML に描画します。
8. fingerprint、処理結果、API usage を SQLite に記録し、変更のない再実行をcacheします。

triage の出力する event_id は必ずローカルの event 記録と突き合わせ、時刻とファイル名はモデル応答ではなく元の event から復元します。triage が失敗した場合も日報は生成され、判定が付かなかったことが日報と終了コード `2` に反映されます。

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
notepad config\scene.yaml
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

## 撮影場所の説明 (scene)

`config/scene.yaml` は、このカメラが普段何を映しているかを記述するファイルです。世帯構成や生活パターンを含むため、Git にもイメージにも入りません（`.gitignore` と `.dockerignore` の両方で除外しています）。compose は `./config` を `/config` に read-only で mount するため、コンテナは `/config/scene.yaml` として読みます。

`config/scene.example.yaml` をコピーして編集してください。全項目が省略可能で、未設定の項目はプロンプトから丸ごと省かれます。ファイル自体が無い場合は警告を出して scene なしで動作します。

| 項目 | 内容 |
| --- | --- |
| `location` | 敷地の種別と周辺環境 |
| `camera_view` | 画角に入る範囲。PTZ で巡回する先も含める |
| `household` | 世帯構成と普段の行動。役割で書き、個人名は不要 |
| `routine_patterns` | ほぼ毎日発生し、指摘不要な行動 |
| `known_vehicles` | 自宅の車両 |
| `expected_visitors` | 配達や収集など想定内の来訪 |
| `notes` | 死角、画角に入る隣家、季節要因など |

外見ではなく振る舞いを書いてください。「白い車が停まっている」より「平日朝7時台から8時台に自転車で出入口へ向かう」「玄関はスマートロックで、スマートフォンを近づけて解錠する」のような記述が、住人と判別不能な人物の切り分けに直接効きます。triage プロンプトは外見（性別、年齢、服装、体格）を判断根拠にしないよう明示的に指示しています。

別の場所へ置く場合は `ANALYZER_SCENE_FILE` で上書きできます。

```
docker compose --env-file .env run --rm -e ANALYZER_SCENE_FILE=/config/scene.local.yaml analyzer
```

scene を変更すると観察プロンプトの内容が変わるため、event cache は自動的に無効化され、次回実行時に対象日が再解析されます。

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

`prompts/` と `templates/` は Dockerfile の `COPY` でイメージに焼き込まれますが、compose では作業ツリーを read-only でマウントして上書きします。プロンプトやテンプレートの編集は再ビルドなしで次回の実行に反映されます。`app/` 以下のコードを変更した場合は再ビルドが必要です。

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
scene_file: scene.yaml
triage:
  enabled: true
  attention_score_threshold: 7
  notable_always_attention: true
  history_days: 14
  max_attention_items: 20
processing:
  continue_on_error: true
```

`triage` は要確認イベントの絞り込みを制御します。`attention_score_threshold` 以上のスコアが付いたイベントに加え、`notable_always_attention` が `true` の間は `notable` が記載されたイベントもスコアに関わらず抽出されます。スコアは単体では校正が甘いため、両方を併用する既定値のまま運用しながら閾値を調整することを想定しています。

要確認が多すぎる場合、原因は閾値ではなく `notable` の質であることが多いです。観察記録は断定を避けるため「〜かは不明」という記述がほぼ全イベントに残り、これを `notable` に転記されると全件が要確認になります。triageプロンプトは未確認の記述を `notable` に書かないよう明示していますが、それでも多い場合は次の2つが効きます。

`routine_explained_score_cap` は、`routine_explanation` が記入され、かつ `notable` が空のイベントについて、スコアの上限をローカルで強制します。既定の `4` は閾値 `7` を下回るため、平常の行動と説明がついたイベントは細部が未確認でも要確認になりません。`notable` が記入されている場合は上限を適用しないので、平常の行動の最中に起きた例外は取りこぼしません。

`group_related_events` は、triageが同一の出来事と判断した複数イベントを1件にまとめます。来客の到着から見送りまでのように、1つの出来事が複数の録画に分割されるケースで件数が水増しされるのを防ぎます。まとめられたイベントは最も注目すべき1件の関連動画として日報に表示され、評価件数としては引き続き全件が計上されます。

`scene` の `expected_visitors` に季節の記載（お盆、正月など）があると、triageは対象日の日付と突き合わせます。この時期の来客や複数人の出入りが不審者として抽出されるのを避けるには、季節要因をここに書いておいてください。

`history_days` は、普段との差分を判断させるために triage へ渡す過去の日報の日数です。過去の日報が無い状態でも動作しますが、「その日だけ普段と違う」種類の指摘は履歴が溜まるまで出にくくなります。渡すのは各日の概要と傾向、要注意記載だけで、過去のevent JSON全体は送信しません。

`enabled: false` にすると triage を実行せず、要確認セクションのない従来どおりの日報になります。

`gpt-5.6-luna` はコストを抑えた既定値です。model ID、利用可否、料金はOpenAI側で変更され得るため、実行前に契約Projectで確認してください。料金見積りは `config/config.yaml` の単価による参考値で、請求画面が正です。

## 定期実行

Windows Task Schedulerでは、04:00に次を起動します。

```text
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\path\to\reolink\scripts\run_daily.ps1" -ProjectPath "C:\path\to\reolink" -LogPath "C:\reolink-analysis\daily.log"
```

wrapperは `docker compose run --rm analyzer` を1回実行し、その終了コードを返します。特定日のbackfillには `-Date 2026-08-16`、明示的な再解析には `-Force` を追加します。

Linux移行用の例は `deploy/reolink-analyzer.service`、`.timer`、`crontab.example` にあります。systemd timerとcronはどちらか一方だけを使用してください。

## トラブルシューティング

### `exec /usr/local/bin/reolink-analyzer: no such file or directory`

ビルドは成功するのに実行時にこのエラーが出る場合、entrypointスクリプトの改行コードがCRLFになっています。`#!/bin/sh` の行末にCRが付くと、カーネルは `/bin/sh\r` というインタプリタを探して失敗し、このメッセージを返します。スクリプト自体は存在しています。

Git for Windows は既定で `core.autocrlf=true` のため、チェックアウト時にLFがCRLFへ変換されるのが原因です。`.gitattributes` で `*.sh` をLF固定にしているので、既存のワーキングツリーを再正規化してください。

```
git rm --cached -r .
git reset --hard
docker compose build --no-cache
```

Dockerfile側でもビルド時にCRを除去するため、リポジトリを更新すれば再ビルドだけで解消します。

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
- APIへ送信するのは縮小JPEGとpromptです。triage段と日報段ではevent JSONとテキストだけを送信します。
- `config/scene.yaml` には世帯構成と生活パターンが含まれます。`.gitignore` と `.dockerignore` の両方で除外していますが、`git add -f` やイメージの外部公開では失われる保護なので注意してください。
- scene の内容は毎回のtriageプロンプトに含まれてAPIへ送信されます。送信して差し支えない粒度で記述してください。
- triage は人物を resident / visitor / unknown に分類しますが、外見や属性ではなく振る舞いのみを根拠とするよう指示しています。この分類は確認対象を絞るための目安であり、個人の識別ではありません。
