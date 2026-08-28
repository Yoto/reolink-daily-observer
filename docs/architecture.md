# Architecture

Reolink Daily Observer は、Reolink RLC-823S1 が FTP 転送した MP4 を日単位で観察し、1動画ごとの event JSON と、その日の daily report を生成します。

この文書では処理の構造と、各段を分離している理由を説明します。設定値は [configuration.md](configuration.md)、日々の実行方法は [operations.md](operations.md) を参照してください。

## Design goals

主な設計方針は次のとおりです。

- **観察と判定を分離する。** event JSON は映像から直接確認できる事実だけを記録し、危険性や犯罪可能性の判定を含めません。
- **判定をテキストだけで再実行できるようにする。** triage は event JSON と scene、直近の日報履歴を読み、画像を扱いません。
- **モデル応答をそのまま信頼しない。** event ID、時刻、ファイル名などローカルで検証できる情報は元の event 記録と突き合わせます。
- **入力映像を変更しない。** MP4 は read-only で mount し、移動・変更・削除しません。
- **部分失敗を隠さない。** 一部の動画や triage が失敗しても可能な範囲で日報を生成し、終了コードと出力へ失敗状態を反映します。

## Processing pipeline

1. 対象日の MP4 を列挙し、転送が完了していることを確認します。
2. ffprobe で duration / resolution / fps / codec を取得します。
3. 動画先頭の縮小フレームから昼間かどうかを判定し、解析用フレームの解像度を選択します。
4. ffmpeg で1秒間隔の JPEG を一時抽出します。
5. OpenAI の画像入力と Structured Outputs で、1 MP4 = 1 event JSON を生成します。
6. event JSON、scene、直近の日報履歴を入力に triage を1回実行し、各イベントへ assessment / person_type / notable / anomaly_score などを付与します。
7. 閾値以上のスコア、または notable が記載されたイベントをコード側で要確認として抽出します。
8. event JSON と triage 結果から canonicalな `daily_report.json` を保存します。HTML表示はviewerがJSONから動的に生成します。
9. fingerprint、処理結果、API usage を SQLite に記録し、変更のない再実行を cache します。

## Observation

観察段だけが動画フレームを扱います。ここでは「何が映っているか」を客観的に記録し、住人か不審者かといった意味付けを行いません。

長い動画はフレームを時系列 chunk に分割し、各 chunk を観察した後、テキストだけの synthesis で1つの event へ統合します。一時 JPEG はコンテナの `/tmp` に置き、処理後に削除します。

日単位の通常実行では未処理動画の観察リクエストを Batch API にまとめて投入します。単一動画の `analyze-video` はプロンプト評価用途のため常に同期実行です。

## Triage

triage は画像を扱わず、次のテキスト情報だけから確認優先度を判断します。

- 当日の event JSON
- 撮影場所と定型行動を記述した scene
- 直近の日報履歴

これにより、scene や triage prompt、抽出閾値を変更した場合に、動画を再解析せず判定だけをやり直せます。

人物分類は resident / visitor / unknown を使用しますが、外見や属性ではなく振る舞いのみを根拠にするよう prompt で制約しています。これは確認対象を絞るための分類であり、個人識別ではありません。

## Attention selection

要確認イベントの最終抽出はモデルに任せずコード側で機械的に行います。既定では次のどちらかを満たすイベントを抽出します。

- anomaly_score が `triage.attention_score_threshold` 以上
- `triage.notable_always_attention` が有効で、notable が記載されている

平常行動として説明されたイベントには `routine_explained_score_cap` を適用できます。また、同じ訪問が複数の録画へ分割された場合は `group_related_events` により日報上でまとめられます。

詳しい調整方針は [tuning-and-evaluation.md](tuning-and-evaluation.md) を参照してください。

## Integrity and failure handling

triage が返した event_id は必ずローカルの event 記録と突き合わせます。時刻とファイル名はモデル応答から採用せず、元の event から復元します。

triage 応答から一部 event ID が欠落した場合、その event は異常度7の「未評価」として要確認へ追加されます。`max_attention_items` の上限でも省略しません。

triage 全体が失敗した場合も、可能なら日報を生成し、判定が付かなかったことを日報と終了コード `2` へ反映します。

## Cache and fingerprints

処理結果と fingerprint は SQLite に保存されます。入力や解析条件が変わらなければ event observation は cache され、再課金を避けられます。

観察 prompt や、観察入力へ影響する設定を変更した場合は fingerprint が変わり、必要な event が再解析されます。明示的に cache を無視したい場合だけ `--force` を使用します。

## Why Batch is used only for observation

費用の大半は画像を含む event observation です。また1日の各動画は互いに独立しており、通常実行は前日分を対象とするため、bulk queue の待ち時間を許容できます。

一方、triage と daily report は各1リクエストで、全 event の完了に依存します。これらまで Batch API に載せても削減額は小さく、待ち時間だけがもう1段増えるため同期実行のままです。

Batch API の運用上の注意点は [operations.md](operations.md#batch-api) を参照してください。
