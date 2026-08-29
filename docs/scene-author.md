# scene-author

`scene-author` は、利用者が同じ種類の定型行動だと確認済みの複数動画から、`config/scene.yaml` に手作業で追加するための候補文を生成する補助コマンドです。

## プライバシー境界

- `config/scene.yaml` は読み込みません。
- `scene_file` と inline `scene` は設定読み込み時に破棄します。
- `ANALYZER_SCENE_*` で注入された scene 値も破棄します。
- `scene.yaml` を自動更新する機能はありません。
- APIへ送るのは、指定した動画から抽出したJPEG、利用者が与えた正解ラベル、ファイル名から解析できた録画開始時刻、動画長です。動画ファイル名そのものはpromptへ入れません。
- `--json-output` の出力には観察された生活パターンが含まれるため、公開リポジトリではなく `/data/state` などのprivateな領域へ保存してください。

## 使い方

同じ定型行動だと確認済みの動画を2本以上指定します。

```bash
docker compose run --rm analyzer scene-author suggest "新聞配達" \
  /data/input/2026/08/20/example1.mp4 \
  /data/input/2026/08/21/example2.mp4 \
  /data/input/2026/08/22/example3.mp4
```

既定では各動画から最大24フレームを時間方向に分散して抽出します。通常運用より精度優先の補助処理なので、`frames.max_long_edge_px` の解像度を使い、昼間向けの解像度削減は適用しません。変更する場合:

```bash
docker compose run --rm analyzer scene-author suggest "新聞配達" \
  --frames-per-video 16 \
  /data/input/2026/08/20/example1.mp4 \
  /data/input/2026/08/21/example2.mp4
```

構造化された中間観察と利用量も保存する場合:

```bash
docker compose run --rm analyzer scene-author suggest "新聞配達" \
  --json-output /data/state/scene-author-newspaper.json \
  /data/input/2026/08/20/example1.mp4 \
  /data/input/2026/08/21/example2.mp4
```

## 出力

各動画を個別に観察した後、テキストだけの統合リクエストを1回行います。正例がN本ならAPIリクエストは原則N+1回です。

最終出力は次の区分を持ちます。

- 共通して強く使える特徴: 複数の正例に繰り返し現れ、routine照合に使いやすい特徴
- 補助的に使える特徴: 一部の正例でのみ見られるため必須条件にしない特徴
- sceneの条件に使わない方がよい特徴: 不安定、判別困難、外見依存などの特徴
- scene.yaml追加候補: 人間が確認・修正して `routine_patterns` などへ転記するための自然文

候補は自動適用しません。場所の呼称や実際の生活上の意味が正しいか人間が確認してから `scene.yaml` に反映し、その後 `triage-eval run` で回帰確認してください。
