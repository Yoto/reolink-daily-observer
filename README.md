# Reolink Daily Observer PoC

Reolink RLC-823S1 が FTP 転送した MP4 を日単位で観察し、1動画ごとの event JSON と、その日の `daily_report.json` を生成する PoC です。日報はFastAPI viewerがJSONから動的に表示します。

観察と判定を分離しており、event JSON は映像から直接確認できる事実だけを記録します。その後、画像を扱わない triage が event JSON、撮影場所の説明、過去の日報から住人が確認すべきイベントを抽出します。

```text
MP4
 ↓
observation ──→ event JSON
                   ↓
                 triage
                   ↓
              daily report
```

既定の実行環境は Linux + Docker Engine です。入力 MP4 は read-only で mount し、移動・変更・削除しません。

詳しい設計は [docs/architecture.md](docs/architecture.md) を参照してください。

## Quick Start

前提:

- Docker Engine が起動していること
- `docker compose version` が成功すること
- OpenAI API を使う場合は専用 Project で発行した API key があること

初期設定:

```bash
cp .env.example .env
cp config/scene.example.yaml config/scene.yaml
sudo install -d -o 10001 -g 10001 -m 0750 /srv/reolink-analysis/output /srv/reolink-analysis/state
vi .env
vi config/scene.yaml
```

`.env` で少なくとも入力・出力パスと `OPENAI_API_KEY` を確認してください。API key はソースコードや YAML へ書かず、ローカルの `.env` だけに保存します。

イメージを構築します。

```bash
docker compose --env-file .env build
```

前日分を処理します。

```bash
docker compose --env-file .env run --rm analyzer
```

日報viewerを起動します。ブラウザから `http://localhost/`（ポートを変更した場合は
`VIEWER_HTTP_PORT`）へアクセスしてください。

```bash
docker compose --env-file .env up -d viewer nginx
```

viewerは日報JSONだけをread-onlyで読み、MP4はnginxが直接配信します。FastAPIの
8000番ポートはホストへ公開されません。

## Basic Usage

日付指定:

```bash
docker compose --env-file .env run --rm analyzer --date 2026-08-16
```

単一動画:

```bash
docker compose --env-file .env run --rm analyzer analyze-video '/data/input/YYYY/MM/DD/Security camera_00_YYYYMMDDhhmmss.mp4'
```

Batch API を使わず、その場で結果を得る場合:

```bash
docker compose --env-file .env run --rm analyzer --date 2026-08-16 --sync
```

cache を無視して明示的に再解析する場合だけ `--force` を追加します。

定期実行、Batch API の注意点、終了コード、トラブルシューティングは [docs/operations.md](docs/operations.md) を参照してください。

## Output

```text
/srv/reolink-analysis/output/2026-08-16/
├─ events/event_2026-08-16_<hash>.json
└─ daily_report.json

/srv/reolink-analysis/state/state.sqlite
```

## Documentation

| Document | Purpose |
| --- | --- |
| [Architecture](docs/architecture.md) | observation / triage / report の構造と設計理由 |
| [Configuration](docs/configuration.md) | `.env`、`config.yaml`、`scene.yaml` の設定 |
| [Operations](docs/operations.md) | 実行、Batch API、定期実行、障害対応 |
| [Tuning and Evaluation](docs/tuning-and-evaluation.md) | 誤検知の改善方針、triage 回帰評価 |
| [Development](docs/development.md) | ローカル開発、mock、テスト、再ビルド |
| [Viewer](docs/viewer.md) | 日報UI、nginx、権限とセキュリティ設定 |
| [scene-author](docs/scene-author.md) | 複数の実動画から scene 追加候補を作る補助コマンド |

## Privacy

このシステムは防犯カメラ映像と生活パターンを扱います。MP4、event JSON、daily report、state DB、ログ、`config/scene.yaml` は機密データとして扱ってください。

`config/scene.yaml` は `.gitignore` と `.dockerignore` の対象ですが、scene の内容は triage 時に API へ送信されます。送信して差し支えない粒度で記述してください。

API へ送信するデータや scene の詳しい取り扱いは [docs/configuration.md#privacy](docs/configuration.md#privacy) を参照してください。
