# DeckLog2TTS

DECK LOGの公開Deck IDまたは `/view/{DeckID}` URLから、カード画像を取得し、Tabletop Simulator（TTS）のSave JSONへ変換するCLIです。

## クイックスタート（Windows）

Python 3.11以降を推奨します。外部Pythonパッケージは不要です。

### Steam Cloud同期（必須）

2026年10月24日時点で公式のSteam Cloud APIは公開されておらず、[TTS Cloud Sync](https://github.com/Chatanga/TTSCloudSync) の `sync-with-cloud` コマンドを利用する必要があります。GitHubリリースから配布されている `TTSCloudSync-win-x64` などのビルド済みバイナリを取得し、ZIPを展開したディレクトリを `PATH` に追加してください（ソース一式を `ttscloudsync/` 以下に配置しても自動ビルドは行われません）。SteamクライアントでTabletop Simulatorにログインした状態で実行する必要があります。

```cmd
python decklog2tts.py convert 1NWU1J --install
```

完了後、TTSを開き `Games > Save & Load` から `1NWU1J.json` を読み込むと、画像はSteam Cloudから配信されます。

URL入力も可能です。

```cmd
python decklog2tts.py convert "https://decklog.bushiroad.com/view/1NWU1J" --install
```

## 出力

```text
output/
  1NWU1J/
    1NWU1J.json
    manifest.json
    assets/
      DZ-TBP02__dztbp02_004.png
      ...
      card_back.png
cache/
  assets/
    DZ-TBP02__dztbp02_004.png
    ...
    card_back.png
```

`cache/assets` は全デッキで共有するカード画像キャッシュです。同じカードを使う別デッキを生成しても再ダウンロードは発生しません。`--refresh` を付けるとキャッシュを上書きします。容量が肥大化した場合は不要なPNGを削除し、必要に応じて再生成してください。

横向きカード（公式APIで `direction=1` のもの）は自動的に縦長PNGへ回転し、TTS側で縦横比が崩れないよう補正されます。

## HTTP配信について

公式カード画像URLをTTSへ直接指定すると、ブラウザでは表示できてもTTS側で取得エラーになる場合があります。本ツールはSteam Cloud経由で配信される前提でJSONを書き換えるため、`sync-with-cloud` を利用できない環境では生成物を手動で編集し、外部ホスティング（Google Drive等）へ配置するなど別途対応が必要です。

## オプション

- `--install`: Windowsの `Documents\My Games\Tabletop Simulator\Saves\Saved Objects` にSave JSONをコピー
- `--refresh`: 既存の画像キャッシュを再取得
- `--cloud-path`: Steam Cloud上のリモートパスを指定（既定: `decklog2tts/assets`）。環境変数 `DECKLOG2TTS_CLOUD_PATH` でも設定可
- `--cloud-cli`: 利用する `sync-with-cloud` コマンド名またはパスを指定。環境変数 `DECKLOG2TTS_CLOUD_CLI` に設定すると毎回指定不要

```powershell
# 例: 解凍したTTSCloudSyncのディレクトリを指定
$env:DECKLOG2TTS_CLOUD_CLI = 'C:\Tools\TTSCloudSync'
# 例: リモートパスを共通化
$env:DECKLOG2TTS_CLOUD_PATH = 'decklog2tts/assets'
```

PowerShell プロファイルや `.ps1` スクリプトに設定しておけば、`--cloud-cli` を省略しても自動的に反映されます。

`.env` ファイルをプロジェクト直下または実行ディレクトリに置くと、自動で読み込まれます（既存の環境変数が優先されます）。コミット用には空値を並べた `.env.sample` を同梱しているため、運用時は `cp .env.sample .env` などで複製し、各自の環境パスを設定してください。

### 外部CLIの推奨配置

- リリース済みの `TTSCloudSync-win-x64` を任意のツールディレクトリ（例: `C:\Tools\TTSCloudSync`）へ展開し、`sync-with-cloud.exe` にアクセス可能な状態にする。
- 必要に応じて `PATH` を通すか、`.env` / 環境変数 `DECKLOG2TTS_CLOUD_CLI` へディレクトリパスを設定する。
- `ttscloudsync/` 以下のソース一式はリファレンス用途のみで、デフォルトの変換フローでは参照されません。不要であれば削除しても問題ありません。

```text
# 解凍ルートのみ指定すれば、内部の sync-with-cloud.bat が自動検出されます。
DECKLOG2TTS_CLOUD_CLI=C:\Tools\TTSCloudSync
DECKLOG2TTS_CLOUD_PATH=decklog2tts/assets
```

`.env` は UTF-8 で保存し、`#` から始まる行はコメントとして扱われます。

## 注意事項

- 公開デッキのみを対象にしています。
- 画像の再配布や公開ホスティングは、権利者の規約・著作権・利用条件を確認してください。
- APIレスポンスの `card_number` と `img` が一致しないケースがあるため、画像取得には必ず `img` を使用します。
- `card_back.png` は既定ではリポジトリ内の `assets/card_back.png` をコピーします。ファイルが存在しない場合は内蔵ロジックで単色の汎用裏面を生成します。
