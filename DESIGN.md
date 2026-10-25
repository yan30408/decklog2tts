# DeckLog2TTS 設計・引継ぎ資料

## 1. 目的

DECK LOGの公開Deck IDを入力として、以下を自動化する。

1. 公開デッキJSONの取得
2. メインデッキとライドデッキ等の解析
3. 共有キャッシュディレクトリへのカード画像ダウンロード
4. TTS `DeckCustom` Save JSONの生成
5. Steam Cloudから参照可能なアセットURLの組み立て

## 2. 確認済みインターフェース

### デッキ取得

```text
POST https://decklog.bushiroad.com/system/app/api/view/{deck_id}
Body: なし
```

実装では次を付与する。

```text
Accept: application/json, text/plain, */*
Referer: https://decklog.bushiroad.com/view/{deck_id}
User-Agent: 一般的なブラウザUA
```

Cookie、Authorization、CSRFヘッダーには依存しない設計。

### 画像取得

APIの各項目にある `img` を、次の基底URLへ結合する。

```text
https://cf-vanguard.com/wordpress/wp-content/images/cardlist/{img}
```

重要: `card_number` から画像名を再構築しない。例えばカード番号と画像ファイル名の数値が一致しないデータが存在するため、`img` を正とする。

## 3. データマッピング

- `list`: メインデッキ
- `p_list`: ライドデッキ、エネルギージェネレーター等
- `num`: 枚数
- `name`: TTSの `Nickname`
- `card_number`: TTSの `Description`
- `direction`: 真ならTTSの `SidewaysCard`
- `img`: 画像取得元およびローカル資産名

## 4. 出力アーキテクチャ

各Deck IDごとに独立したバンドルを作る。

```text
output/{deck_id}/
  {deck_id}.json
  manifest.json
  assets/
    {set}__{filename}.png
    card_back.png
```

`manifest.json` はトラブルシュートや将来の再生成用に、元データと `asset_base` を保持する。カード画像自体は `cache/assets/{hashed_name}.png` に保存し、デッキ側 `assets/` にはハードリンクまたはコピーを配置する方針とし、将来的にSteam Cloud同期用の共通フォルダへ集約できるようにする。

`convert` 実行時は `manifest.json` に `cloud` エントリが追加され、Steam Cloud上のリモートパス(`path`)と各ファイル名→URLのマッピング(`urls`)を保存する。既定のリモートパスは `decklog2tts/assets` とし、全デッキで共有する。`DECKLOG2TTS_CLOUD_PATH` 環境変数または `.env` で既定値を変更可能。

### 共有キャッシュ

- 取得済みカード画像は `cache/assets/{safe_name}.png` に保存する。
- `download_images` はキャッシュの存在を確認し、未取得時のみネットワークアクセスする。
- デッキ固有バンドルへはハードリンク（Windows対応可なら `os.link`）またはコピーで配置し、Steam Cloud同期前の整合性を保つ。
- APIで `direction=1` かつ横向き（`width > height`）のカードはPNGを90度回転して縦長とし、`SidewaysCard` 設定と組み合わせて縦横比の歪みを防ぐ。

### Steam Cloud同期

- `sync-with-cloud` コマンドを子プロセスとして呼び出し、共有クラウド先（既定では `decklog2tts/assets`）にキャッシュされたPNGを同期する。環境変数 `DECKLOG2TTS_CLOUD_CLI` や `.env` にリリース済み `TTSCloudSync-win-x64` などのバイナリを展開したディレクトリ（例: `C:\Tools\TTSCloudSync`）を設定すると、内部の `sync-with-cloud.bat` / `.exe` を自動検出して CLI 引数を省略できる。リポジトリ直下の `ttscloudsync/` は参照資料用のソースであり、自動ビルドは行わない。
- コマンド出力の `sha1_filename;https://...` 形式を解析し、TTS Save JSON内の `FaceURL` / `BackURL` をSteam Cloud URLへ差し替える。
- 差し替え後のSaveを上書き保存し、`asset_base` もSteam Cloud上のベースURLに更新する。繰り返し実行時は差分アップロードのみが発生するため、同一カードを再利用する際の負荷が大幅に削減される。

### 公式デッキレシピ対応

- `extract_recipe_deck_ids(url)` で公式サイトのHTMLを取得し、`https://decklog.bushiroad.com/view/{DeckID}` 形式のリンクや「DECK LOGコード：XXXX」の表記からDECK LOGコードを抽出する。重複は除外し、抽出できない場合はエラーにする。
- `extract_recipe_sources(url)` では上記に加えて `data-recipe-id="{id}"` を収集し、DECK LOGコードが掲載されていないページでも内部API呼び出し用のIDを取得できるようにした。
- `fetch_recipe_deck(recipe_id)` は `https://cf-vanguard.com/manage/deckrecipe/detail` を呼び出し、`is_ride` でライドデッキを判定、`card_kind` が 4〜9 のカードを横向きカードとして扱う。
- `recipe` サブコマンドは抽出した各Deck IDに対して `convert_single_deck` を順番に呼び出し、続けてレシピIDに対して `convert_recipe_deck` を実行する。同じオプション（`--refresh`、`--install`、`--cloud-path`、`--cloud-cli`）を再利用し、途中でエラーがあっても残りのデッキの処理は続行するが、終了コードは最後に発生した非0値を返す。
- レシピAPI由来の出力は `RECIPE{id}` をDeck IDとし、`deck_meta.name` をタイトルに設定する。
- ページ構造が変わってDeck Logリンクが掲載されなくなった場合に備えて、エラー文言でURL確認と公開状態の確認を促す。

## 5. TTS形式

- メインデッキと `p_list` は別の `DeckCustom` オブジェクトとして出力
- 絵柄ごとに `CustomDeck` IDを1つ割り当てる
- `num` 回だけ同一 `CardID` を `DeckIDs` と `ContainedObjects` に展開
- `FaceURL` は共有キャッシュを基にしたSteam Cloud URL
- `BackURL` は生成した汎用 `card_back.png`

## 6. セキュリティと信頼境界

- Deck IDは英数字のみ許可
- APIの `img` に `..` が含まれる場合は拒否
- ダウンロード結果はPNGシグネチャで検証
- Cookieやユーザーセッションを保存しない
- API応答内の未知フィールドは実行せず、データとしてのみ扱う

## 7. 既知の制約

- `cache/` ディレクトリは徐々に肥大化するため、不要カードの整理を手動で行う運用が必要。
- マルチプレイ共有にはHTTPSで到達可能なアセットホストが必要。
- DECK LOGまたは公式画像サイトの仕様変更時は定数/API処理の更新が必要。
- TTSにおける横向きカードの最終表示は、ゲーム側のレンダリング仕様に依存する。
- 画像の公開再配布可否は本ツールでは判定しない。
- Steam Cloud同期には [TTS Cloud Sync](https://github.com/Chatanga/TTSCloudSync) の `sync-with-cloud` が必要で、SteamクライアントでTTSにログインした状態（2026年10月25日現在）が前提。
- 共有キャッシュの整合性が崩れた場合は `--refresh` を併用して再取得すること。

## 8. エラー処理

- APIがJSONでない: Content-Typeと解析エラーを表示
- `list` がない: 非公開デッキまたはAPI変更として停止
- 画像がPNGでない: URL、Content-Type、サイズを表示して停止
- 既存画像: `--refresh` がなければ再利用

## 9. 拡張候補

優先度順:

1. Tkinter GUI（Deck ID、出力先、変換、サーバー起動）
2. Windows向けPyInstaller単一EXE
3. 複数Deck IDの一括変換
4. HTTPS/S3/Azure Blob等へのアップロードアダプター
5. TTSスプライトシート出力モード
6. 自動テスト（API JSON fixture、TTS schema snapshot、PNG検証）

## 10. テスト観点

- Deck IDと `/view/` URLの正規化
- `list` 50枚、`p_list` 5枚などの枚数合計
- 同一画像の重複ダウンロード抑止
- `card_number` と `img` が不一致でも `img` を使用
- `direction=1` が `SidewaysCard=true`
- キャッシュ利用、`--refresh`
- API 404/403、画像404、HTML応答
- 日本語カード名のUTF-8保持

## 11. 実行例

```cmd
python decklog2tts.py convert 1NWU1J --install
```

```cmd
python decklog2tts.py recipe "https://cf-vanguard.com/deckrecipe/events/areacup2026_s2_sapporo_4th/" --install
```

