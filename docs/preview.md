# Frontend preview environment

preview環境は、PRを作る前に本番の日報と動画でUI変更を確認するための、手動更新する小さなスタックです。

```text
本番checkout (main)                 preview worktree (feature branch)
docker-compose.yml                  docker-compose.preview.yml
analyzer + viewer + nginx           viewer + nginx
viewer image :local                 viewer image :preview
          │                                  │
          ├──── 出力日報 (read-only) ─────────┤
          └──── カメラ動画 (read-only) ───────┘

本番 :80                            preview 127.0.0.1:8081
```

Composeファイルは既定のproject名を`reolink-preview`に固定します。そのためコンテナ、Docker network、viewer image tag、port、working treeは本番と分離されます。Analyzer serviceを含まず、API keyも渡しません。

## 別worktreeを作る

本番checkoutで次を実行します。branch名は実際のfeature branchへ置き換えてください。

```bash
git worktree add -b feature/family-report-ui /opt/reolink-preview main
cd /opt/reolink-preview
cp .env.example .env.preview
chmod 600 .env.preview
vi .env.preview
```

branchがすでにlocalにある場合は`-b`を付けません。

```bash
git worktree add /opt/reolink-preview feature/family-report-ui
```

本番checkoutをfeature branchへ切り替えないでください。Composeは現在のworking treeから設定をbind mountします。専用worktreeに分けることで、後日本番containerを再作成したときにpreview側のコードや設定を取り込む事故を防ぎます。

preview環境が`.env.preview`から使う値は次のとおりです。

```dotenv
CAMERA_INPUT_DIR=/srv/reolink
ANALYSIS_OUTPUT_DIR=/srv/reolink-analysis/output
CAMERA_GID=10010
ANALYSIS_GID=10001
VIEWER_UID=10002
VIEWER_GID=10002
NGINX_UID=10003
NGINX_GID=10003
CAMERA_DATE_LAYOUT=nested
PREVIEW_HTTP_HOST=127.0.0.1
PREVIEW_HTTP_PORT=8081
```

各directoryと配下のファイルには、本番と同じgroup read/search権限が必要です。どちらのbind mountもComposeでread-onlyに指定しています。

## 起動・更新・停止

preview worktreeで起動します。

```bash
docker compose --env-file .env.preview -f docker-compose.preview.yml up -d --build
docker compose --env-file .env.preview -f docker-compose.preview.yml ps
```

既定ではホスト上で`http://127.0.0.1:8081/`を開きます。別のPCから見る場合もlocalhost bindを維持し、SSH tunnelを使えます。

```bash
ssh -L 8081:127.0.0.1:8081 user@nucbox
```

接続元PCで`http://127.0.0.1:8081/`を開きます。スマートフォンなどLAN内の端末でresponsive表示を確認するときは、ホストfirewallで8081番を信頼するLANだけに制限してから`PREVIEW_HTTP_HOST=0.0.0.0`へ変更します。認証機能のないカメラviewerをrouterからInternetへport-forwardしないでください。

UIを変更するたびに同じスタックを再buildします。

```bash
docker compose --env-file .env.preview -f docker-compose.preview.yml up -d --build
```

previewのcontainerとnetworkだけを停止・削除します。

```bash
docker compose --env-file .env.preview -f docker-compose.preview.yml down
```

本番とはCompose projectとimage tagが異なるため、本番スタックには影響しません。

## データ境界

第一版では、表示処理が実際に使うデータだけをmountします。

| 本番データ | preview側の参照元 | Access |
| --- | --- | --- |
| `ANALYSIS_OUTPUT_DIR`配下の日報JSON | viewer | read-only |
| `CAMERA_INPUT_DIR`配下のMP4 | nginx | read-only |
| `ANALYSIS_STATE_DIR`配下のstate SQLite DB | なし | mountしない |

現在のviewerはAnalyzerのstate DBを参照しません。mountしないことで機密データの露出を減らし、本番書き込み中のSQLite WALやlockに関する問題も避けます。将来viewerがDBを必要とする場合は、live state directoryをそのまま追加せず、journal modeを確認したうえでread-only snapshotまたはpreview専用DBを使用します。

共有するnginx設定は、日付scopeの`.mp4` pathだけを公開し、動画へのGET以外のmethodとsymlinkを拒否します。previewの両containerも本番と同じnon-root user、read-only root filesystem、capability drop、`no-new-privileges`を維持します。

## Review loop

UIの微調整は同じfeature branchとpreview stack上で繰り返します。

```text
実装 → preview再build → 目視レビュー → 修正 → preview再build
```

目視レビューが完了してからPRを作ります。途中のcommitはcheckpointとして残して構いません。PR作成時にsquashまたは整理します。
