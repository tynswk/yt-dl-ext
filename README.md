# yt-dl-ext

開いている YouTube 動画を **mp4 / mp3 / wav** で保存する Chrome 拡張。

常駐プロセスは無い。ダウンロードするときだけ Chrome が Python（yt-dlp のラッパー）を
起動し、用が済めば数秒後に自動で終了する。Native Messaging を使っているので、
サーバを手で起動する必要も無い。

## セットアップ（順番が大事）

### 1. 拡張を読み込む

1. Chrome で `chrome://extensions` を開く
2. 右上の「デベロッパーモード」をオン
3. 「パッケージ化されていない拡張機能を読み込む」→ このフォルダの `extension` を選択
4. カードに表示される **ID**（`abcdefgh...` の32文字）を確認しておく

### 2. ネイティブホストを登録する

```
powershell -ExecutionPolicy Bypass -File install-nativehost.ps1
```

拡張IDは Chrome のプロファイルから自動検出する。見つからなければ入力を求められるので、
手順1で見た ID を貼る（`-ExtensionId <ID>` で渡してもよい）。

書き込むのは `HKCU` 配下のレジストリキーと `%LOCALAPPDATA%\yt-dl-ext\host` だけなので
管理者権限は不要。Chrome と Edge の両方に登録する。

そのため、このフォルダ（リポジトリ）はどこに置いてもよい。移動したら手順2をやり直す。

**登録後に Chrome を一度再起動する。** ネイティブホストの登録は起動時に読まれる。

解除は `uninstall-nativehost.ps1`。

依存：`yt-dlp`（`pip install -U yt-dlp`）と `ffmpeg`。どちらも PATH にあれば自動で見つかる。

## 動作環境

- Windows 10 / 11（レジストリ登録と `explorer` 連携を使うため Windows 専用）
- Python 3.9 以降
- Google Chrome または Microsoft Edge（`chrome://extensions` が使えるもの）

## 使い方

YouTube の動画ページでツールバーのアイコンをクリック → 形式と品質を選んで
「ダウンロード」。

保存先は既定で `%USERPROFILE%\Downloads\yt-dl-ext`。ポップアップの入力欄を書き換えて Enter（または
フォーカスを外す）と変更でき、隣のフォルダアイコンでその場所を開ける。
指定先は絶対パスであること・実際に書き込めることを確認してから保存するので、
打ち間違えても設定が壊れることはない（その場でエラーを出して元に戻す）。
設定は `config.json`（ホストと同じフォルダ）に入る。保存先が消えていた場合は
既定 → 一時フォルダ配下の `yt-dl-ext\` の順に落ちるので、落ちずに動き続ける。

ダウンロード中にポップアップを閉じても中断しない（接続を持っているのは
サービスワーカーなので）。完了するとアイコンに ✓ バッジが出る。開き直せば進捗の続きが見える。

| 形式 | 中身 |
|---|---|
| MP4 | 映像+音声を結合。best / 1080p / 720p / 480p 上限を選択可 |
| MP3 | 音声のみ。VBR 最高音質 / 標準 / 軽量 |
| WAV | 音声のみ、16bit PCM 無圧縮 |

## yt-dlp の自動更新

YouTube の仕様変更で yt-dlp が動かなくなることが定期的にある。Web のダウンローダが
「今日は使えない」状態になるのもだいたいこれ。自分で気にしなくていいように2段構えにしてある。

- **週1回**：ホストが起動したとき、前回の更新から7日以上経っていれば裏で `pip install -U yt-dlp`
  を走らせる。ホストは用が済むと数秒で終了するので、pip は**切り離して**起動する
  （道連れにして中断させると壊れたインストールが残るため）。
- **失敗したとき**：ダウンロードが失敗したら、その場で更新して**1回だけ**やり直す。
  ポップアップには「yt-dlp を更新しています…」と出る。
  実際にバージョンが上がったときだけ再試行するので、動画が非公開などの直しようがない
  失敗で無駄に粘ることはない。直近10分に試して変化が無ければ pip 自体を省略する。

最終確認日時とバージョンは `last_update.json`（ホストと同じフォルダ）に記録される。
手で更新したいときは `pip install -U yt-dlp`。

## 構成

```
yt-dl-ext/
├─ install-nativehost.ps1    レジストリ登録＋ホストマニフェスト生成
├─ uninstall-nativehost.ps1  解除
├─ host/
│  ├─ native_host.py         yt-dlp を叩く。Chrome が起動し、切断で終了
│  └─ run_host.bat           Chrome から呼ばれる入口（manifest は引数を取れない）
└─ extension/
   ├─ manifest.json          MV3
   ├─ background.js          ネイティブ接続を保持。用が済んだら切ってホストを落とす
   ├─ popup.html / .css / .js
   └─ icons/
```

`host\config.json`（保存先）、`host\last_update.json`（yt-dlp の更新履歴）、
`host\debug.log`、`host\com.ytdlext.host.json`（インストーラが生成するマニフェスト）は
実行時に作られるもので、環境ごとの値が入るため Git 管理外にしてある。

拡張 → ホストのメッセージは `health` / `info` / `download` / `reveal`、
ホスト → 拡張は `health` / `info` / `started` / `progress` / `done` / `error`。

## 詰まったときは

環境変数 `YTDLEXT_DEBUG=1` を設定して Chrome を起動すると、`host\debug.log` に
やり取りと yt-dlp の生出力が記録される。Chrome から起動されたホストは標準エラーが
どこにも出ないので、これが唯一の手がかりになる。

ホスト側で踏んだ罠：**yt-dlp に stdin を継承させてはいけない**。Chrome とのパイプが
子プロセスに渡ると yt-dlp が一切出力せず無言で固まる（100秒以上）。
`stdin=subprocess.DEVNULL` が必須。

## 注意

Chrome ウェブストアはダウンローダ拡張の掲載を禁止しているので、この拡張は
デベロッパーモードでの読み込み専用。また YouTube の利用規約はコンテンツの
ダウンロードを原則禁止している。自分がアップロードした動画、ライセンスが
明示されているもの、著作権の切れたものなど、権利上問題のない範囲で使うこと。

## ライセンス

MIT（[LICENSE](LICENSE)）。
