# Tuning and Evaluation

この文書は、誤検知や取りこぼしを見つけた後に判定精度を継続的に改善するための方針と回帰評価手順をまとめます。

設定値の意味は [configuration.md](configuration.md)、scene の候補文生成は [scene-author.md](scene-author.md) を参照してください。

## Improvement loop

基本の改善ループは次のとおりです。

```text
誤判定を確認
  ↓
triage-eval fixture を追加
  ↓
現在の状態で FAIL を再現
  ↓
原因を分類
  ├─ scene に定型行動の説明が不足
  ├─ triage prompt の一般則が不足
  └─ config / threshold の問題
  ↓
最小限の修正
  ↓
triage-eval run
  ↓
既存ケースも含めて PASS を確認
```

実データで失敗を再現してから修正することで、単発の誤検知を抑えた結果として別の異常を見逃すリグレッションを防ぎます。

## What belongs in regression fixtures

fixture へ取り込む主な対象は、**正常だと分かっているのに月1回以上の頻度で要確認へ上がる事象**です。

それより稀な事象は未知性を保つため、原則として fixture 化せず要確認に残します。また、高頻度でも正常だと確認できていない事象を抑制ケースにしてはいけません。

fixture は映像を含みませんが、家庭で観察された行動を含むため、公開リポジトリではなく既定の `/data/state/triage-eval` に保存します。

## Add a triage regression case

既存 event JSON から1ケースを追加します。対象 event と同じディレクトリにある同日 event、および当時の日報履歴も自動的にコピーし、本番の日次 triage に近い入力を再現します。

次の例は、毎日の新聞配達を visitor として説明し、要確認へ出さないことを期待するケースです。`add` は API を呼びません。

```bash
docker compose run --rm analyzer triage-eval add \
  --event /data/output/2026-08-21/events/event_EXAMPLE.json \
  --id newspaper-delivery-001 \
  --description "早朝の定型的な新聞配達" \
  --frequency daily \
  --no-attention \
  --person-type visitor \
  --routine present \
  --routine-contains "新聞配達" \
  --notable absent \
  --score-max 4
```

同じ ID を現在の同日 event・履歴で作り直す場合は `--replace` を付けます。対象 event だけの小さな単体確認をしたい場合に限り `--isolated` を使用してください。

## Run regression evaluation

現在の config、scene、triage prompt で全ケースを再評価します。

```bash
docker compose run --rm analyzer triage-eval run
```

各ケースは `PASS` / `FAIL` で表示されます。失敗時は attention、person_type、routine_explanation、notable、anomaly_score のどの期待値が外れたかを表示します。

1ケースにつき triage の同期リクエストを1回行います。全件成功なら終了コード `0`、期待値違反または処理失敗があれば `2` を返します。

CI などで結果を保存する場合:

```bash
docker compose run --rm analyzer triage-eval run \
  --json-output /data/state/triage-eval-result.json
```

応答から一部 event ID が欠落した場合もケース全体を FAIL にします。本番の日次処理でも欠落 event は「未評価」として必ず要確認へ上げるため、モデル応答の欠落が平常 event として黙って流れることはありません。

## Choosing what to change

### Change scene when the behavior is locally normal

scene は「この場所・この家庭では普通」というローカルな事実を書く場所です。

例:

- 毎朝ほぼ同じ時間帯に来る新聞配達
- 家族が決まった経路で自転車を出し入れする
- 定期的なゴミ収集や宅配
- お盆や正月など特定時期に増える来客

同じ種類の定型行動を複数の実動画から整理したい場合は [scene-author.md](scene-author.md) を使用できます。scene-author は候補文を生成するだけで `scene.yaml` を自動更新しません。人間が確認してから反映し、その後 `triage-eval run` を実行します。

### Change the triage prompt when the rule should generalize

特定の家庭だけでなく、多くの scene に共通して適用すべき判断原則は triage prompt 側で扱います。

例:

- 「確認できない」という記述だけを notable にしない
- routine と説明できる event は、不明点が残るだけで高スコアにしない
- 外見や属性を resident / visitor 判定の根拠にしない

scene に一般則を大量に書くと、個別環境の説明と判定ロジックが混ざるため避けます。

### Change thresholds only after checking semantics

要確認が多すぎる場合、最初から `attention_score_threshold` を上げるのではなく、まず notable と routine_explanation の内容を確認します。

観察記録には断定を避けるため「〜かは不明」のような記述が残ります。これを triage が notable へ転記すると、多数の event が要確認になります。これは閾値ではなく判定意味論の問題です。

`routine_explained_score_cap` は routine_explanation があり、notable が空の event にだけ適用されます。平常行動の途中に本当の例外があり notable が付いた場合は cap されないため、この仕組みを優先して使います。

## Related-event grouping

`group_related_events` は、1回の来客が複数 clip に分かれた場合などに、同じ出来事を日報上でまとめます。

これは誤検知抑制ではなく表示上の重複除去です。評価件数としては全 event が残るため、「1件にまとめたから判定自体を捨てる」という動作にはなりません。

## Resolution reduction evaluation

昼間の解像度削減は triage とは別の品質・コスト調整です。

判定は動画先頭の複数縮小フレームから彩度と暗部率を測定し、明らかな昼間だけ低い解像度を使います。判定不能・夜間候補・測定失敗は高解像度側へ倒します。

閾値を変更する場合は、実際の昼・夜・ライト点灯後など複数条件の動画から測定値を集め、散布図などで分布を確認してから決めてください。単一動画に合わせた閾値変更は避けます。

## Recommended change discipline

- 実例で FAIL を再現してから直す
- scene はローカルな定型行動、prompt は一般則に限定する
- 稀な未知事象を安易に抑制しない
- threshold は意味論の問題を直した後に調整する
- 修正後は対象ケースだけでなく全 regression fixture を実行する
