# Operations

この文書はセットアップ、日々の実行、Batch API、定期実行、障害対応をまとめた運用手順書です。

設定値の意味は [configuration.md](configuration.md)、システム構成は [architecture.md](architecture.md) を参照してください。

## Prerequisites

- Docker Engine が起動していること
- `docker compose version` が成功すること
- OpenAI API を使う場合は専用 Project で発行した API key があること

## Initial setup

プロジェクトルートで次を実行します。

```bash
cp .env.example .env
sudo install -d -o 10001 -g 10001 -m 0750 /srv/reolink-analysis/output /srv/reolink-analysis/state
cp config/scene.example.yaml config/scene.yaml
vi .env
vi config/scene.yaml
```

API key は `.env` の `OPENAI_API_KEY` にだけ保存してください。

イメージを構築します。

```bash
docker compose --env-file .env build
```

## Daily run

前日分を処理します。

```bash
docker compose --env-file .env run --rm analyzer
```

日付を指定する場合:

```bash
docker compose --env-file .env run --rm analyzer --date 2026-08-16
```

cache を無視して明示的に再解析する場合だけ `--force` を追加します。

## Synchronous mode

通常の日次実行では event observation に Batch API を使用します。当日中に結果が必要な場合は `--sync` を付けて同期実行します。

```bash
docker compose --env-file .env run --rm analyzer --date 2026-08-16 --sync
```

同期実行は待ち時間が短い代わりに Batch API の割引を利用しません。

## Analyze a single video

単一動画の解析は常に同期実行です。

```bash
docker compose --env-file .env run --rm analyzer analyze-video '/data/input/YYYY/MM/DD/Security camera_00_YYYYMMDDhhmmss.mp4'
```

prompt や scene の変更結果を素早く確認するときに使用します。

## Outputs

```text
/srv/reolink-analysis/output/2026-08-16/
├─ events/event_2026-08-16_<hash>.json
└─ daily_report.json

/srv/reolink-analysis/state/state.sqlite
```

`daily_report.json` はcache、過去日報、viewerに共通するcanonicalな日報です。表示方法は [Daily report viewer](viewer.md) を参照してください。

## Source retention

保持期限を有効にする前に、削除候補をdry-runで確認します。

```bash
RETENTION_ENABLED=true docker compose --env-file .env run --rm retention retention --dry-run
```

結果にはcutoff、対象日数、ファイル数、容量、処理未完了でskipした日数が出ます。
想定どおりなら `.env` を更新します。

```dotenv
RETENTION_ENABLED=true
RETENTION_MONTHS=3
RETENTION_UID=<camera date directoryを削除できるUID>
RETENTION_GID=<そのprimary GID>
```

手動で実削除だけを行う場合:

```bash
docker compose --env-file .env run --rm retention retention
```

削除対象は保持期限を完全に経過した日付directoryのMP4だけです。既定では
日報とevent JSONで全MP4の処理完了とfingerprint一致を確認します。symlink、
未知のdirectory、日報・event JSON・SQLiteは削除しません。空になった日付、月、
年directoryだけを後から取り除きます。
日報とevent JSONは残るため過去の日報は閲覧できますが、削除済みの日付では
元動画を再生できなくなります。

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | 成功 |
| `1` | 設定・日報生成などの致命的失敗 |
| `2` | 一部動画の失敗、triage の部分失敗、または保留 |

## Batch API

日単位の通常実行では、画像を送る event observation だけを OpenAI Batch API に投入します。triage、daily report、長い動画の synthesis は同期実行です。

1回の batch に入る件数と容量には上限があり、超える場合は自動的に複数 batch へ分割して、すべての完了を待ちます。

### Temporary storage

同期実行では1動画ずつフレームを抽出して処理後に削除します。一方 batch では、投入時に未処理動画すべての JPEG を用意するため、`paths.temp` に1日分のフレームが一時的に存在します。

compose では `/tmp` を tmpfs にしているため、`ANALYZER_TMPFS_SIZE` で十分な容量を確保してください。既定値は `4g` です。tmpfs は RAM を消費します。

JSONL はリクエスト単位でストリームするため、全リクエストを同時にメモリへ保持することはありません。

### Interrupted runs

batch id はプロセスとログにだけ保持されます。batch 投入後にコンテナが落ちると、その batch の結果を次回実行が自動回収することはありません。

ログには `batch submitted batch_id=...` を残します。再実行すると未完了の動画を再投入する可能性がありますが、すでに完了し cache された event は再処理されません。

### Timeout

`genai.batch.max_wait_sec` を超えても完了しない batch は cancel し、該当動画を失敗として記録します。可能な範囲で日報を生成し、終了コード `2` を返します。

### Uploaded files

入力 JSONL は batch が terminal state になった後に削除します。1日分の入力は大きくなり得るため、Project 側の file 容量を圧迫しないためです。

結果ファイルとエラーファイルは監査用に残します。入力 JSONL を残したい場合は次を設定します。

```yaml
genai:
  batch:
    delete_input_file: false
```

### Switching Batch off

次のいずれかで同期実行に戻せます。

```text
config: genai.batch.enabled: false
.env:  GENAI_BATCH_ENABLED=false
CLI:   --sync
```

`mock` provider は設定に関わらず同期実行です。

## Scheduled execution

### Linux

systemd timer と cron の例は次にあります。

- `deploy/reolink-analyzer.service`
- `deploy/reolink-analyzer.timer`
- `deploy/crontab.example`

systemd timer と cron はどちらか一方だけを使用してください。

systemd版では `scripts/run-scheduled.sh` が同じdeploy lockの内側でanalyzerを実行し、
成功または部分成功（exit 2）の後にretentionを実行します。致命的な解析失敗
（exit 1）では削除を開始しません。retentionは期限切れの各日を独立に検証するため、
当日の部分失敗が過去の処理済みデータの削除を妨げません。

既に `deploy/reolink-analyzer.override.conf` をインストール済みの環境では、変更後の
unit commandを反映するため、review済みcheckoutから `deploy/install-cd.sh` を再実行し、
その後にtimer設定を確認してください。自動deployはroot所有のsystemd drop-inを
書き換えません。

### Windows Task Scheduler

04:00 に前日分を処理する例です。

```text
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\path\to\reolink\scripts\run_daily.ps1" -ProjectPath "C:\path\to\reolink" -LogPath "C:\reolink-analysis\daily.log"
```

wrapper は通常の日次実行でanalyzerに続いてopt-inのretentionを実行し、終了コードを返します。
`-Date` を指定したbackfillではretentionを実行しません。

追加オプション:

- backfill: `-Date 2026-08-16`
- 明示的な再解析: `-Force`
- Batch API を使わず即時実行: `-Sync`

既定では Batch API の完了を待つため1回の実行が長時間続くことがあります。Task Scheduler 側に短い実行時間上限を設定しないでください。

## Troubleshooting

### `exec /usr/local/bin/reolink-analyzer: no such file or directory`

ビルドは成功するのに実行時にこのエラーが出る場合、entrypoint script の改行コードが CRLF になっている可能性があります。`#!/bin/sh` の行末に CR が付くと、カーネルは `/bin/sh\r` を探して失敗します。

Git for Windows の `core.autocrlf=true` による変換が原因の場合は、`.gitattributes` の LF 固定を反映するためワーキングツリーを再正規化します。

```text
git rm --cached -r .
git reset --hard
docker compose build --no-cache
```

Dockerfile 側でもビルド時に CR を除去するため、リポジトリ更新後は再ビルドだけで解消する場合があります。
