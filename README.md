# OctantCompass

> **To use BeatOctant** (install, known issues): [Getting Started](https://github.com/BeatOctant/OctantSpecs/blob/main/GettingStarted_en.md) · [はじめかた（日本語）](https://github.com/BeatOctant/OctantSpecs/blob/main/GettingStarted_ja.md)
>
> BeatOctant is written mostly with AI coding agents (Anthropic's Claude). The maintainer decides what to build and reviews every change. It does not generate maps; it recommends existing maps from BeatSaver. ([作り方（AI の使い方）](https://github.com/BeatOctant/OctantSpecs/blob/main/README.md#作り方ai-の使い方))
>
> OctantCompass is the local recommendation server for the Beat Saber mods OctantPath (Beat Saber on PC) and OctantPath for Quest (OctantPathAndroid, Beat Saber on Meta Quest).
>
> BeatOctant is an unofficial fan project for Beat Saber. It is not affiliated with, endorsed by, or sponsored by Beat Games or Meta. Beat Saber and Meta Quest are trademarks of their respective owners. Beat Games・Meta とは関係のない非公式の MOD・ツールです。
>
> 報告と案は Discord の BeatOctant サーバー（バグは `#bug-reports`、案は `#feedback-ideas`）で受けます。外からの pull request は受けていません。

Beat Saber の MOD [OctantPath](https://github.com/BeatOctant/OctantPath) に推薦を返す、ゲームの外で動くローカルの推薦サーバーです。仕様、契約、実装の条件はすべて [OctantSpecs](https://github.com/BeatOctant/OctantSpecs) にあり、入口は [OctantCompass 法](https://github.com/BeatOctant/OctantSpecs/blob/main/octantcompass/law.md) です。BeatOctant 全体の中での位置は [OctantSpecs の概要](https://github.com/BeatOctant/OctantSpecs#概要)にあります。

## 組み合わせる版

支えるのは、各部品の最新の版どうしの組み合わせだけです（[decisions.md の全体と段階](https://github.com/BeatOctant/OctantSpecs/blob/main/decisions.md#全体と段階)、2026-10-22）。この README を書いた時点の相手は次のとおりです。

| 相手 | 版 | つなぐもの |
| --- | --- | --- |
| OctantPath（MOD と OctantPath Desktop）、OctantPathAndroid | 最新 | [契約3（OctantPath ↔ OctantCompass）](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/mod-api.md) 2.0、[契約6（つなぐ側 ↔ OctantCompass）](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/launch.md) 2.0 |
| OctantVector | v0.9.2 のタグ（`pyproject.toml` の `tool.uv.sources`。範囲は `>=0.9.0,<0.10`） | [契約1 推薦側（OctantCompass → OctantVector）](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/engine-runtime.md) 1.0 |
| 配布 DB（OctantHarvest が作る） | 最新 | [契約2（DB 生成 ↔ 推薦コア）](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/vector-db.md) 1.1、[契約5（OctantHarvest → 配布先 → OctantCompass）](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/distribution.md) 1.1 |
| OctantSpecs | `OCTANTSPECS_REF`（`main`） | テストが読む契約の例 |

## OctantSpecs の取得

このリポジトリが合わせている OctantSpecs の版は `OCTANTSPECS_REF` にあります。次のコマンドで、その版を `.octantspecs/` に取れます（ネットワークと、OctantSpecs のリポジトリへの git のアクセスが要ります）。

```sh
bash scripts/fetch-octantspecs.sh
```

テストは `.octantspecs/` がなくても動きます。テストが読む契約の例は `tests/examples/` に写してあります。

## 開発環境

Python 3.11.4 以上（OctantVector の下限）と `uv` がある環境で、次を実行します。

```sh
bash scripts/setup-python.sh
.venv/bin/python -m pytest -q
```

`gui`（窓）を開発環境で開くには `--extra gui` を足します（pywebview）。Linux では PyGObject と pycairo を手元で組み立てるので、gobject-introspection と cairo の開発用ファイル（Debian・Ubuntu は `libgirepository1.0-dev libcairo2-dev pkg-config`）と、WebKitGTK 4.1 とその typelib（`gir1.2-webkit2-4.1`）が要ります。`serve` とテストには要りません。

依存はこのリポジトリ専用の `.venv/`、取得用キャッシュは `.uv-cache/` に置きます。システムの Python へパッケージを追加せず、Python 本体のダウンロードも行いません。

GitHub Actions（[test.yml](.github/workflows/test.yml)）が、pull request ごとに、Python 3.11 で同じ 2 つのコマンドを実行します。文書だけの変更では動かず、同じ pull request に続けて push すると前の実行を止めます。OctantVector（`octantvector` extra）と OctantSpecs は取りません。

## 起動と管理

```sh
.venv/bin/octantcompass gui
.venv/bin/octantcompass serve
.venv/bin/octantcompass pair code
.venv/bin/octantcompass env list
.venv/bin/octantcompass env revoke <環境ID>
.venv/bin/octantcompass db switch /absolute/path/to/new-db
.venv/bin/octantcompass db import /absolute/path/to/2026.10.15.full.tar
.venv/bin/octantcompass db update [--check] [--index <https の URL>]
.venv/bin/octantcompass shortcut
```

コマンドは `git remote add` と同じく「名詞 動詞」の形です（2026-10-14）。`octantcompass --help` と `octantcompass <コマンド> --help` が、それぞれ何をするかを出します。端末からコマンドを付けずに起動すると help を出します。`gui` は OctantCompass の窓を開き、動いていなければ起動します。`serve` は窓を出さずに前で動き続け、Ctrl+C か SIGTERM で終わります。

ファイルの置き場所は、各コマンドに `--data-dir` を付けるとそのフォルダ 1 つにまとめます。付けなければ、Linux では XDG の決まりに分けて置きます（2026-10-14）。Windows と macOS はこれまでどおりユーザーごとのデータフォルダ 1 つです。

| 置き場所 | ファイル | Linux の既定 |
|---|---|---|
| 設定 | `settings.json`、`window.json`（窓で選んだ言語とテーマ、閉じたときの窓の位置と大きさ） | `~/.config/OctantCompass`（`$XDG_CONFIG_HOME`） |
| 状態（消えると困るもの） | `pairing.json`（ペアリングした環境とトークンのハッシュ。書き換えの鍵に `.pairing.json.lock`）、`db-selection.json`（使っている DB）、`db-request.json`（コマンドからの切り替え要求） | `~/.local/state/OctantCompass`（`$XDG_STATE_HOME`） |
| データ | `db/`、`dbs/`（索引から取り込んだ DB）、`downloads/`（取り込みの間だけ部分のファイルを置く） | `~/.local/share/OctantCompass`（`$XDG_DATA_HOME`） |
| 動いている間だけのもの | `connection.json`、`instance.lock`、`window.lock`（窓を 1 つにする）、`window-request.json`（2 つ目の `gui` が窓を前に出す頼み）、`start.log`（窓からの最後の起動の出力） | `$XDG_RUNTIME_DIR/OctantCompass`（なければ状態と同じ場所） |

ファイルはどれもテキストです。OctantPath から受け取った signal はファイルに書かず、動いている間だけメモリに持ちます。OctantCompass を起動し直すと、OctantPath が正本を丸ごと送り直します（2026-10-22）。

通常起動は OctantVector を使います。OctantVector が入っていない、版が合わない、DB がないときは health が `degraded` になり、signal はその状態でも保存します。人工データで接続を試すときは `serve --fake-engine` を使えます。`POST /v1/profile` は認証した環境の履歴と要求の `params` を使い、OctantVector の要約を返します。

OctantPath とは通信だけでつながります（[契約6（client to OctantCompass）2.0](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/launch.md)）。既定のポートは 23287 で、同じ PC の OctantPath はここを探します。ペアリングは同じ PC でも、窓か `pair code` が出す合言葉を OctantPath に 1 回入れます。`connection.json` は OctantCompass 自身の多重起動の防止と窓のためのもので、合言葉は入れません。

常駐していても DB（読み込むと 2GB ほど）は持ち続けません。起動したときは読み込まず、ペアリングした環境から要求が来たら読み込み、`idle_minutes`（既定 30 分）要求がなければ手放します。その間 `/v1/health` は `loading` を返し、トークンのない要求では読み込みません。`idle_minutes` を 0 にすると、起動したときに読み込んで手放しません。

窓の詳細設定の「ログイン時に起動する」（`start_at_login`、既定は切）を入れると、ログインしたときに窓を出さずに `serve` で起動します。Linux は systemd のユーザーユニット（`~/.config/systemd/user/octantcompass.service`、`default.target` に enable）に書くので、`systemctl --user status octantcompass` で状態を、`systemctl --user stop octantcompass` で止められます。systemd のユーザーマネージャーがない環境では `~/.config/autostart/octantcompass.desktop` に書きます。Windows はスタートアップのショートカットに書きます。切ると消します。起動のたびに書き直すので、実行ファイルを置き換えたり動かしたりしても次のログインから合います。Windows の分はまだ Windows で試していません。

`db switch` は指定フォルダを OctantVector で検証し、同じデータフォルダのホストへ切り替え要求を記録します。動作中のホストは `poll_seconds` の間隔で確認し、自分でも検証を通してから採用します。反映は `/v1/health` の `db.dbVersion` で確認できます。切り替え要求と採用済みの DB は別に記録し、失敗した要求は次の起動でも採用済みの DB を置き換えません。次の起動は採用済みの DB を使い、未処理の要求があれば処理します。`serve --db` は起動時にその指定を優先し、起動前からある要求は処理しません。

### 設定

設定の既定値は、パッケージに同梱した [`src/octantcompass/defaults.json`](src/octantcompass/defaults.json) にあります（単体実行ファイルにも入ります）。`settings.json` には利用者が変えた値だけを置き、OctantCompass は既定値にその値を重ねて読みます。読むときにファイルへ書き戻すことはなく、`settings.json` がなくても既定値で動きます。窓で値を保存すると、その項目だけを書き、既定値と同じ値なら項目を消します。既定値と同じ値で固定したい（既定値が後で変わっても変えたくない）ときは、`settings.json` に手で書きます。読むときもほかの項目を保存するときも、その項目は書き換えません。窓からその項目を既定値と同じ値で保存したときだけ消えます。`defaults.json` にないキーは読みません。

待ち受けるアドレス（`host`）の既定は 127.0.0.1 で、この PC からだけ届きます。0.0.0.0 や :: にすると同じネットワークのほかの端末からも届きますが、通信は平文の HTTP なので、ペアリングの合言葉とトークンは暗号化されずに流れます。家など信頼できるネットワークだけで使ってください。そのときは起動と再起動のたびにログに警告を出します。Quest は USB（adb reverse）でつなげば 127.0.0.1 のままで使えます。

窓に出すのは利用者が変える値だけです。内部の値（待ち時間の `health_timeout`・`startup_timeout`・`shutdown_timeout`、確認の間隔の `poll_seconds`、索引の大きさの上限 `index_max_megabytes`）は画面に出しません。変えるときは `settings.json` に手で書き、窓の「再起動」か次の起動から使われます。`db`（DB のフォルダ。null ならデータフォルダの `db/`）はこれと違い、「再起動」では読み直さず、起動のときも、DB を切り替えたことも取り込んだこともない（状態のフォルダに `db-selection.json` がない）ときにだけ使います。一度でも切り替えるか取り込むと、それからは採用済みの DB を使います。ふだんは DB の切り替えで選びます。

### 窓

OctantCompass の画面は、ブラウザではなく OctantCompass 自身の窓に出ます（pywebview。Windows は Edge WebView2、Linux は WebKitGTK 4.1。2026-10-18）。タスクバーやアプリの一覧には OctantCompass として出ます。単体実行ファイルのダブルクリック（端末のない起動。Windows はコマンドを付けない起動すべて）は `gui` と同じに動きます。`shortcut` は、Linux ではアプリ一覧（`~/.local/share/applications/octantcompass.desktop`）に、Windows ではスタートメニューに、窓を開くショートカットを作ります。Windows の分はまだ Windows で試していません。

`gui` は窓を開き、同じデータフォルダの OctantCompass（ログイン時に起動したものを含む）が動いていなければ、`gui` に付けた引数で `serve` を別のプロセスとして起動します。窓を閉じても OctantCompass は動き続け、OctantPath に推薦を返します。止めるのは窓の「停止」です。窓は 1 つのデータフォルダに 1 つで、2 つ目の `gui` は開いている窓を前に出して終わります。言語とテーマ（ダーク、ライト、OS に従う自動）は窓で選び、次の窓からも使われます（`settings.json` と同じ設定のフォルダの `window.json`）。窓は前に閉じたときの位置と大きさで開き、最大化していたら最大化して開きます（2026-10-21。`placement.py`）。その位置がどの画面にも入らないとき（画面を外したときなど）は、初めてのときと同じく最初の画面の真ん中に開きます。Linux の Wayland ではプログラムが窓の位置を決められないので、大きさと最大化だけを戻し、位置はデスクトップが決めます。窓の中は 1280×720（ページの CSS の px。`minsize.py`）より小さくなりません（2026-10-19。OctantPath Desktop と同じ）。ページは全体では動かず、欄の中だけが動きます（1 つの欄に動く枠は 1 つ）。画面がそれより小さいとき（1920×1080 を拡大率 150% で使うなど）は窓を画面の作業領域に収め、ページごと少し動きます。

窓は設定、起動・停止・再起動、DB を受け持つので、OctantCompass が止まっていても設定を変え、DB を切り替え・取り込めます（2026-10-19）。ポートがほかのプログラムに使われているなどで起動できなかったときは、その理由（起動の出力の終わり。動いている間だけのもののフォルダの `start.log`）を「状態」に出すので、設定を直してもう一度「起動」を押します。止まっている間は、動いている OctantCompass にしかない欄（つないだ OctantPath、ログ）にそう書きます。

DB の欄は、使っている DB のフォルダ（動いていればそのフォルダ、止まっていれば次の起動で使うフォルダ）のマニフェストを OctantVector の `read_info` で読んで出します。DB は読み込まないので、OctantCompass が DB を読み込む前も、止まっている間も出ます。切り替え・取り込み・索引からの取り込みは、窓のプロセスが `db switch`・`db import`・`db update` と同じ仕組みでします。OctantCompass が開くのと同じように確かめてから、状態のフォルダの `db-request.json` で OctantCompass に渡すので、動いている OctantCompass は `poll_seconds` のうちに切り替え、止まっていれば次の起動から使います。渡したがまだ使われていない DB は「次に使う DB」に出ます。作業は一度に 1 つで、終わるまで窓は閉じません（途中で止まってしまうため。索引の確認だけのときは閉じます）。索引からの取り込みは、部分のファイルを取っている間なら「やめる」でやめられ、今の DB はそのままです。窓の作業は OctantCompass のログには出ず、署名の警告は窓の結果に出ます。

「状態」のタブに状態、つないだ OctantPath、手札、ログを、「詳細設定」のタブに DB の中身と切り替え・取り込み、利用者が変える設定（待ち受けるアドレス、ポートの固定、索引、署名など）と、それぞれの「変更」を出します。変えた値は `settings.json` に書きます（[設定](#設定)）。動いている間は、待ち受けるアドレスとポートは「再起動」で、ほかの項目は保存したときから使います。止まっている間は、次の起動から使います。見た目は OctantPath Desktop に合わせています。色の見え方が違っても使えるよう、状態の色は赤と緑で分けず（青緑・黄・朱で、明るさも変える）、接続の印の形（塗った丸・輪・ひし形）と文でも分け、文字は背景に対して 4.5:1 以上にしています（2026-10-19）。

- 状態（ready / loading / degraded と理由、DB の版と置き場所、待ち受けアドレス、起動時刻）と、DB の切り替え（`db switch` と同じ検証）
- DB のフォルダと tar は、パスを入力するか「選ぶ…」で OS の選択画面から選べます
- つないだ OctantPath の一覧と取り消し、ペアリングの合言葉の発行
- 手札（OctantVector が返した一覧をそのまま見せるだけ）
- OctantCompass の動きのログ（メモリに直近 `log_lines` 行。トークンと合言葉は書かない）
- 起動、停止と再起動（再起動はプロセスを起こし直さず、設定を読み直して待ち受けだけを張り直します。DB とペアリングはそのままです。アドレスかポートを保存した後は「再起動して反映する」が出ます。張り直せなければ前のアドレスで動き続け、理由を窓に出します。窓は `connection.json` で今のアドレスを知るので、ポートを変えてもそのまま使えます）

窓のページは HTTP では配らず、窓のプロセスが持っています。窓の Python（`window.py`、`manager.py`）は `settings.json`、起動・停止、DB を自分で扱い、動いている OctantCompass にしかないもの（状態、ペアリング、ログ、停止と再起動）は、その管理 API（`/manage/`、`manage.py`）を呼びます。管理 API は 127.0.0.1（と ::1）からだけ受け付けます。使うのはこの PC の窓の Python だけでページからは使わないので、要求の `Host` がこの PC の名前（127.0.0.1、localhost、::1）であることを確かめ、ブラウザがページから送るときに付ける `Origin` があれば断ります。そのうえで、起動ごとに作る鍵を `connection.json`（持ち主だけが読める）に置き、窓はそれを `X-OctantCompass-Manage-Key` で送ります。Quest を USB（adb reverse）でつなぐと Quest のアプリも 127.0.0.1 から届くので、鍵のない要求は断ります（2026-10-23。それまでは鍵なし、2026-10-13 決定）。待ち受けを 127.0.0.1 より広げていても、ほかの端末やブラウザのページからは使えません。管理 API は契約3（OctantPath to OctantCompass）の外にあり、推薦の一覧や試し引きは持ちません（OctantPath Desktop の役目です）。言語は英語と日本語で、文は `src/octantcompass/ui/lang/<言語>.json` にあります。

DB の更新は、[OctantCompass 法の 6](https://github.com/BeatOctant/OctantSpecs/blob/main/octantcompass/law.md#6-db-の更新)のとおり、ローカルフォルダの切り替え（`db switch`）と、契約5（OctantHarvest to OctantCompass）の索引からの取り込み（`db update`、窓の「索引から取り込む」）があります。索引は設定の `index_url`（https の URL）か `--index` で指します。索引も部分のファイルも https でしか取らず、ほかのスキーム（http、file、ローカルのパス）や https の外への転送は、何も取る前か受け取る前に断ります（契約5 の取得の決まり、2026-10-16。索引は署名されないので、途中で索引ごと差し替えられないようにするため）。取るものは OctantVector の `plan_update` が決め、取ったファイルを `apply_update` で検証してデータフォルダの `dbs/<dbVersion>` に書き、開けてから切り替えます。失敗したら今の DB を使い続けます。手元で作った DB は、索引を通さず DB の切り替え（`db switch`）で使います。公開（Dev 版）の DB は 1 つの tar（`<dbVersion>.full.tar`。OctantHarvest の `handoff` が書き、このリポジトリの Releases に置く）で、`db import` か窓の「ファイルから取り込む」で使います。OctantVector の `import_archive` がマニフェストと照らして展開し、署名を確かめます。展開先はデータフォルダの `dbs/` で、名前は tar のファイル名から付けます。そのあとは索引からの取り込みと同じで、開けてから切り替え、失敗したら今の DB を使い続けます。署名がない、知らない鍵、合わないなどで OctantVector が警告を出した版も使いますが、その警告は窓の取り込みの結果と、`db update` と `db import` の出力（`signatureWarnings`）に出ます。索引と部分のファイルは `OctantCompass/<版> ( https://bsky.app/profile/beatoctant.bsky.social )` の User-Agent で取ります。索引は `index_max_megabytes` まで、部分のファイルは索引に書かれた大きさまでしか読まず、それを超えたら取り込みをやめます。索引に書かれた部分の大きさの合計がデータフォルダの空きより大きければ、何も取らずにやめます。部分のファイルに URL がない索引は、索引と同じ場所にある名前のファイルとして取ります（[契約5](https://github.com/BeatOctant/OctantSpecs/blob/main/contracts/distribution.md) の索引の読み方）。決まった間隔での自動の確認はまだありません。

## 単体実行ファイル

対象 OS 上で、ビルド用の依存、窓、OctantVector を入れて作成します。何を入れるかは [`scripts/octantcompass.spec`](scripts/octantcompass.spec) にあります。

```sh
bash scripts/setup-python.sh --extra build --extra gui --extra octantvector
.venv/bin/python scripts/build-standalone.py
dist/octantcompass serve
```

成果物は `dist/octantcompass`（Windows は `dist/octantcompass.exe`）です。Python、窓、推薦用の依存を含みます。DB は別に用意します。同梱したものとそのライセンスは、ビルドのときに `build/octantcompass/THIRD-PARTY-NOTICES.txt` に書き出し（[`scripts/third_party_notices.py`](scripts/third_party_notices.py)）、実行ファイルにも入れます。Release には実行ファイルと並べて置きます。GNU readline（GPL-3.0）は使わないので入れません。

Linux の実行ファイルは、窓に OS の WebKitGTK 4.1 と GTK 3 を使います（Debian・Ubuntu は `libwebkit2gtk-4.1-0`、Fedora は `webkit2gtk4.1`、Arch は `webkit2gtk-4.1`）。WebKitGTK はページを WebKitGTK と一緒に入ったプログラムで動かし、それは OS のライブラリとしか動かないので、実行ファイルには PyGObject と typelib だけを入れ、WebKitGTK と GTK が読むライブラリ（と、Python も使う OpenSSL、zlib、libstdc++ など）は入れずに OS のものを使います。ビルドする PC にも WebKitGTK 4.1 とその typelib が要ります。Ubuntu 24.04 で試しました。ほかのディストリビューションでも動くと見ていますが、試していません（推測）。Windows は Windows 10 と 11 に入っている WebView2 を使います。

GitHub Actions（[build.yml](.github/workflows/build.yml)）が Linux と Windows で作り、下のテストで起動を確かめます。成果物を置くのは `v*` のタグと手動の実行のときだけで、1 日で消えます。`v*` のタグでは、両方を下書きの GitHub Release に置きます。動くのは、ビルドにかかわるファイルを変えた pull request、`v*` のタグ、Actions の画面からの手動の実行です。開発用には `build-standalone.py --without-octantvector` で代役用の実行ファイルを作れます。

ビルド済みの成果物を検証するテストは、次のように指定すると動きます。

```sh
OCTANTCOMPASS_TEST_EXECUTABLE=dist/octantcompass .venv/bin/python -m pytest tests/test_standalone.py -q
```

## OctantVector アダプタの結合テスト

作業ディレクトリ内の `.venv/` に、固定した OctantVector（リリースのタグか、リリースの間は 1 つのコミット。`pyproject.toml` の `tool.uv.sources`）から推薦側の依存だけを入れます。

```sh
bash scripts/setup-python.sh --extra octantvector
OCTANTCOMPASS_TEST_DB=/absolute/path/to/test-db .venv/bin/python -m pytest tests/test_octantvector.py -q
.venv/bin/octantcompass serve --db /absolute/path/to/test-db
```

`OCTANTCOMPASS_TEST_DB` はテスト用 DB のフォルダです。テストでは DB の内部ファイルを読みません。未指定、または OctantVector が未インストールなら、本物との共通テストだけスキップします。版の不一致、DB の欠落・検証失敗で health が `degraded` になり、ペアリングと signal の受付は続けます。

設定と接続情報の扱いは [OctantCompass 法](https://github.com/BeatOctant/OctantSpecs/blob/main/octantcompass/law.md)を参照してください。テスト例の取得元は [契約1（OctantCompass to OctantVector）](tests/examples/contract1/SOURCE) と [契約3（OctantPath to OctantCompass）](tests/examples/contract3/SOURCE) の SOURCE に記録しています。

## ライセンス

[MIT](LICENSE)（2026-10-12）。
