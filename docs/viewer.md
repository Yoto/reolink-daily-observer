# Daily report viewer

日報UIは同じrepository内の独立コンテナとして動作します。analyzerとviewerの契約は
`YYYY-MM-DD/daily_report.json`だけで、viewerからanalyzerを起動したり内部関数を
importしたりしません。

```text
browser
  │ :80 (VIEWER_HTTP_PORT)
  ▼
nginx (non-root :8080)
  ├─ /, /report/* ──→ viewer:8000 ──RO──→ output
  └─ /videos/* ─────────────────────RO──→ camera input

analyzer ──RO──→ camera input
         ├─RW──→ output
         └─RW──→ state
```

## Start and stop

`.env.example`を`.env`へコピーしてパスとIDを設定した後、常駐サービスを起動します。

```bash
docker compose --env-file .env up -d --build viewer nginx
```

停止は次のとおりです。

```bash
docker compose --env-file .env stop nginx viewer
```

`/`は利用可能な最新日へ移動します。`/report/YYYY-MM-DD`は指定日を表示し、画面上の
前後リンクと日付入力から切り替えられます。動画リンクはnginxの
`/videos/YYYY/MM/DD/<file>.mp4`（`CAMERA_DATE_LAYOUT=flat`なら
`/videos/YYYY-MM-DD/<file>.mp4`）を開きます。

## Linux permissions

サービスのUIDは互いに分け、mountへのアクセスだけを補助グループで付与します。

| Service | camera | output | state |
| --- | --- | --- | --- |
| analyzer | read (`CAMERA_GID`) | read/write (`ANALYSIS_GID`) | read/write |
| viewer | none | read (`ANALYSIS_GID`) | none |
| nginx | read (`CAMERA_GID`) | none | none |

カメラrootと各日付directoryには対象グループのsearch権限、MP4にはread権限が必要です。
`ANALYSIS_GID`は`ANALYZER_GID`と同じ値にし、viewerをその補助グループへ参加させます。
analyzerの`ANALYZER_UMASK=0027`により、新しいreportはグループから読める一方で
world-readableにはなりません。`.env`自体は`chmod 600 .env`で保護してください。

Docker Desktopではbind mountのID変換がプラットフォーム側で処理されるため、既定IDを
そのまま利用できます。

## Security boundary

- ホストへ公開するのはnginxだけです。viewerとanalyzerには`ports`がありません。
- 三サービスともnon-root、read-only root filesystem、`cap_drop: ALL`、
  `no-new-privileges`で動作します。
- viewer/nginxは内部frontend networkだけに置き、analyzerのAPI通信networkから分離します。
- OpenAI API keyはanalyzerへだけ渡されます。
- viewerは日付をdate型でparseし、日付directory・JSON内の日付・動画pathを検証します。
  reportのsymlinkは追跡しません。
- Jinja2 autoescapeを維持し、AI生成文をHTMLとして扱いません。テンプレートで`safe`を
  使用しないでください。
- nginxは日付scopeのMP4 URLだけを配信し、symlinkを拒否します。CSP、frame拒否、
  MIME sniffing拒否などのsecurity headerも付与します。
- API docsとCORSは有効化していません。viewerはproxy headerを信頼しません。

このUIには認証機能を含めていません。防犯カメラ映像を扱うため、routerから直接Internetへ
port-forwardせず、LAN firewallでIoT/guest networkから分離してください。外出先から見る
場合はVPN、Tailscale、WireGuardなどのprivate network経由にします。
