# codex用rootless開発環境

本番と同じホストで開発する場合も、本番checkoutとsystem Dockerを使用しません。
以下は専用ユーザー`codex`（UID/GID 10005）で確認した構成です。

## Checkoutと固定worktree

```bash
git clone https://github.com/Yoto/reolink-daily-observer.git /home/codex/camera/reolink-daily-observer
cd /home/codex/camera/reolink-daily-observer
git worktree add --detach /home/codex/camera/reolink-dev origin/main
cd /home/codex/camera/reolink-dev
uv sync --frozen --extra dev --extra viewer
```

以後はこの固定worktreeで`bash scripts/dev/frontend-start <short-name>`を使います。
本番checkoutからworktreeを作る必要はありません。

## Rootless Docker

管理者が`uidmap`を導入し、`/etc/subuid`と`/etc/subgid`に専用ユーザーの範囲を割り当てます。
既存の割り当ては変更しません。ログアウト後とホスト起動時にも稼働させる場合は、管理者が
`sudo loginctl enable-linger codex`を実行します。

Dockerのrootless extrasが導入済みなら、`codex`のSSHセッションで次を実行します。

```bash
dockerd-rootless-setuptool.sh check
dockerd-rootless-setuptool.sh install
export DOCKER_HOST=unix:///run/user/10005/docker.sock
docker info --format '{{json .SecurityOptions}} {{.DockerRootDir}}'
```

`name=rootless`と`/home/codex/.local/share/docker`を確認します。
ユーザーサービスは`systemctl --user status docker`で確認できます。
system Dockerを停止したり、`codex`を`docker`グループへ追加したりしません。

## Preview用のデータコピー

rootlessではホストの補助グループ`camera` / `reolink-analysis`はコンテナへそのまま引き継がれません。
本番の数値GIDを`group_add`に指定しても別のホストGIDへ変換されるため、本番データの
read-only bind mountで読み取りエラーになる場合があります。

この構成では、ホストの`codex`権限で読める完了日の日報と参照動画を、次の専用領域へコピーします。
本番の権限は変更せず、本番stateは読み取りもコピーも行いません。

```text
/home/codex/camera/dev-data/             # 0700、codex所有、Git管理外
  preview/output/YYYY-MM-DD/daily_report.json
  preview/output/YYYY-MM-DD/family_report.json
  preview/input/YYYY/MM/DD/<参照動画>.mp4
  analyzer/output/                      # 開発解析用。previewコピーと別
  analyzer/state/                       # 開発専用state
  cache/
```

必要な日だけをコピーし、シンボリックリンクを辿らないでください。
コピーは`codex:codex`所有、preview以下のディレクトリは0750、ファイルは0640にします。
コンテナのGID 0は、このrootless環境ではホストの`codex`グループに対応します。
コンテナのUIDは非rootを維持できます。ホストのroot権限を与える設定ではありません。

コピーは自動更新されません。別の日のレビュー時は、その日の日報と参照動画を追加コピーします。
実データやローカル設定はコミットしません。

## 既存ハーネスの実行

固定worktreeの`.env`（0600、Git管理外）を次のように設定します。
既存スクリプトは`.env`を読み、`.env.preview`は自動では読みません。

```dotenv
DOCKER_HOST=unix:///run/user/10005/docker.sock
COMPOSE_PROJECT_NAME=reolink-codex-preview
CAMERA_INPUT_DIR=/home/codex/camera/dev-data/preview/input
ANALYSIS_OUTPUT_DIR=/home/codex/camera/dev-data/preview/output
VIEWER_UID=10002
VIEWER_GID=0
NGINX_UID=10003
NGINX_GID=0
CAMERA_GID=0
ANALYSIS_GID=0
CAMERA_DATE_LAYOUT=nested
PREVIEW_HTTP_HOST=127.0.0.1
PREVIEW_HTTP_PORT=8081
```

```bash
cd /home/codex/camera/reolink-dev
export DOCKER_HOST=unix:///run/user/10005/docker.sock
bash scripts/dev/frontend-check
bash scripts/dev/preview-up
docker compose -f docker-compose.preview.yml ps
curl --fail http://127.0.0.1:8081/healthz
```

viewerとnginxがhealthyになることに加え、日報表示と動画Range配信を確認します。
両データmountは既存Composeの`read_only: true`を維持します。
終了時は同じworktreeで`bash scripts/dev/preview-down`を使います。

WindowsからはSSH tunnelを開き、ブラウザーで`http://127.0.0.1:8081/`へアクセスできます。

```powershell
ssh -N -L 8081:127.0.0.1:8081 nucbox-codex
```

解析の開発では、別の環境設定でoutput/stateを`dev-data/analyzer/`以下へ指定し、
`GENAI_PROVIDER=mock`を使います。preview用outputコピーを解析先として再利用しません。

## GitHub

ユーザー領域にGitHub CLIを導入し、`gh auth login --hostname github.com --git-protocol https --web`で認証後、
`gh auth setup-git`を実行します。Gitの作者名とメールは開発checkoutのローカル設定へ保存します。

変更をcommitした後、既存の`bash scripts/dev/frontend-pr`でテスト、push、PR作成ができます。
UI変更の場合は、引き続き[frontend-review skill](../.agents/skills/frontend-review/SKILL.md)の目視承認手順に従います。
