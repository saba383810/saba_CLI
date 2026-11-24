# saba-cli iTerm2 統合パネル 設計

## 0. ゴール / 非ゴール / 用語

用語

- **session**: 今動いている AI エージェント（claude / codex）のセッション。iTerm2 のペイン単位で数え、1 タブに 2 つあれば 2 つ。登録は要らず、自動で検出する
- **profile（tab profile）**: 保存したタブ構成。左右ペインの dir とコマンド、分割比だけを持つ。押すたびに新しいタブを開くショートカットで、AI エージェントとも session とも関係しない
- **group**: sessions と profiles で共通の、色付きの見出し。session は dir ルール（自動）と手動移動で、profile は手動だけで所属が決まる

ゴール

- iTerm2 ウィンドウの右端に常に出ているパネル。タブより上の階層にあり、表示・非表示を切り替えられる
- 稼働中の session（最大 5 程度）の状態を一覧表示する。状態は run / ask / done / idle の 4 つで、それぞれの件数も出す。クリックでそのペインへ移動する
- session の履歴を保存し、パネルから再開できる
- profile を手動で保存し、パネルで押すだけでタブを開けるようにする。「cd して claude を開いて右に split」の手間をなくすのが目的
- `saba-cli open <dir>` / `saba-cli add <dir>`

非ゴール（v1）

- iTerm2 以外の端末、SSH 先のエージェント
- transcript 全文をパネルに映すこと（必要になったらリンクで外部ビューアを開く）
- 起動時のタブの自動復元。profile は押したときだけ開く

## 1. パネルの置き場所

| 案 | タブより上 | 起動時に自動で出る | 表示切替 | 実装量 |
|---|---|---|---|---|
| **A. iTerm2 Toolbelt の WebView Tool（採用）** | ○ ウィンドウ右端に固定 | ○ `saba-cli panel start` | ○ ⌘⇧B（View > Show Toolbelt） | 小 |
| B. 専用ウィンドウを右端に置き curses TUI | △ 別ウィンドウ | △ 位置合わせが壊れやすい | ○ | 中 |
| C. 各タブを split してパネル用ペインを作る | × タブの中 | ○ | △ | 小 |
| D. Swift の NSPanel を iTerm2 に追従させる | ○ | ○ launchd | ○ | 大（AX API で追従が必要） |

A を採用する。Toolbelt は iTerm2 ウィンドウの右側にあるサイドバーで、ウィンドウ単位、つまり全タブ共通で表示される。「右端」「タブより上」「出したり消したり」を iTerm2 の標準機能だけで満たせる。
中身は WKWebView なので情報を多めに載せられる。クリックすると JS から localhost の API を呼び、iTerm2 Python API でペインをアクティブにしたり、タブを作ったりする。

制約

- 幅はドラッグで調整する。Toolbelt の他のツール（Profiles, Notes など）は手で外しておく
- Toolbelt は右側固定で、左には置けない（iTerm2 に位置の設定がない）
- ウィンドウが複数あると、各ウィンドウに同じパネルが出る。中身は全ウィンドウ共通の一覧で、session をクリックするとそのペインがあるウィンドウへ移動する。profile は今のキーウィンドウにタブを開く

## 2. 全体構成

```
 ┌────────────────────────── iTerm2 window ───────────────────────────┐
 │ Tab: saba_cli                                    │ Toolbelt        │
 │ ┌─────────────────────┬──────────────┐           │ ┌─────────────┐ │
 │ │ claude (session)    │ $ fish       │           │ │ saba-cli    │ │
 │ └──────────┬──────────┴──────────────┘           │ │ (WKWebView) │ │
 └────────────┼─────────────────────────────────────┴─┴──────▲──────┴─┘
   hooks: stdin JSON + $ITERM_SESSION_ID          HTTP + SSE (127.0.0.1)
              ▼                                               │
 saba-cli hook ──write──► SQLite (WAL) ◄──read/write── saba-panel daemon
              └──UDP ping (変更通知)──────────────────────►  (saba-cli panel start,
 saba-cli open/add/save/profile ──HTTP (token)────────────►   iterm2 Python API)
```

| コンポーネント | 役割 | 依存 |
|---|---|---|
| saba-cli（既存 CLI） | `open/add/save/profile/group/ls/history/panel/hook` を追加 | stdlib のみ（現状維持） |
| saba-panel daemon | Toolbelt 登録、ペインの走査と session 検出、profile からのタブ生成、ペインのアクティブ化、HTTP+SSE、状態集約 | `iterm2`。専用 venv にだけ入れる（pyproject は変更しない。§9 参照） |
| Store | SQLite (WAL)。hook・CLI・daemon で共有 | stdlib `sqlite3` |
| Agent adapters | claude と codex それぞれの「hook イベント→状態」「履歴読み取り」「resume コマンド」 | stdlib |
| Panel UI | 単一 HTML + vanilla JS + CSS。パッケージに同梱し、ビルド工程なし | なし |

## 3. データモデル

```sql
groups(                         -- sessions と profiles で共通
  id TEXT PRIMARY KEY,
  name TEXT UNIQUE,
  color TEXT,                   -- red|orange|green|cyan|blue|purple|gray（hex でなくキーで持つ）
  sort INT,
  collapsed INT DEFAULT 0,          -- profiles 欄で畳んでいるか。全ウィンドウのパネルで共有する
  collapsed_sessions INT DEFAULT 0  -- sessions 欄で畳んでいるか。欄ごとに別に持つ
)

group_dirs(                     -- session を自動でグループに入れる dir ルール
  group_id TEXT REFERENCES groups(id) ON DELETE CASCADE,
  dir TEXT UNIQUE               -- 1 つの dir は 1 グループだけ。ホーム配下は `~` 形式・末尾 `/` なしで保存し、比較時に展開する
)

session_group_overrides(        -- session を手動で移したときの記憶。cwd 単位
  cwd TEXT PRIMARY KEY,         -- 絶対パス、末尾 `/` なし。完全一致でだけ効く（配下には効かない）
  group_id TEXT REFERENCES groups(id) ON DELETE CASCADE   -- NULL = 明示的に ungrouped（dir ルールより優先）
)

profiles(                       -- tab profile。タブを開くショートカット。エージェントとは無関係
  id TEXT PRIMARY KEY,
  name TEXT UNIQUE,
  group_id TEXT REFERENCES groups(id) ON DELETE SET NULL,   -- 所属は 0〜1 個
  left_cwd TEXT NOT NULL,       -- `~` はそのまま保存し、開くときに展開する
  left_cmd TEXT,                -- 例: "claude --continue"。空ならシェルのみ
  right_cwd TEXT,               -- NULL なら 1 ペインのタブ
  right_cmd TEXT,               -- 空ならシェルのみ（例: yazi）
  ratio INT DEFAULT 50,         -- 左ペインの幅 %（実測では 50:50 が多い）
  sort INT,                     -- グループ内の並び
  created_at, last_used         -- last_used は最後に開いた時刻。一度も開いていなければ NULL
)

agent_sessions(                 -- 稼働中の session と履歴。1 行 = エージェントの 1 セッション
  id TEXT PRIMARY KEY,          -- エージェント側の session_id
  agent TEXT, cwd,
  iterm_session_id,             -- ペインの id（ITERM_SESSION_ID の UUID 部分）
  iterm_tab_id,
  state TEXT,                   -- run | ask | done | idle | ended
  state_since, current_tool, current_arg,
  title TEXT,                   -- 最初の prompt の先頭部分
  last_prompt TEXT, last_message TEXT,   -- どちらも先頭 200 文字に切り詰める
  transcript_path TEXT, git_branch TEXT,
  started_at, updated_at, ended_at
)

events(id, agent_session_id, ts, kind, payload_json)  -- 生イベント。デバッグとタイムライン用。30 日で削除
```

- profiles と agent_sessions の間に関係はない。profile から開いたタブで claude が動けば、それが session として出るだけ
- session の所属グループは保存せず、表示のたびに cwd から決める（§6.5）。順に、手動の記憶（`session_group_overrides` の cwd 完全一致）→ `group_dirs` の最長一致（`cwd == dir` か `dir/` で始まる）→ ungrouped。group を消すと、その group を指す手動の記憶も消える
- 旧案の workspaces / bindings と、user var（`user.saba_ws`）でタブと workspace を対応付ける仕組みは廃止した。タブとの対応が要るのは session だけで、hooks の `ITERM_SESSION_ID` で取れる
- 状態ファイル: `~/.local/state/saba-cli/`（`saba.db`、`panel.json` に port/token/pid、`panel.log`）
- 設定ファイル: `~/.config/saba-cli/config.json`。requires-python が 3.9 なので tomllib は使わない
- マイグレーションは `PRAGMA user_version` で管理する

## 4. エージェント状態の取得

session はペイン単位で扱う。1 タブに claude が 2 つあれば、それぞれ別の行・別の状態になる。

### 4.1 Claude Code（hooks が主経路）

`~/.claude/settings.json` に `saba-cli hook claude` を追記する。既存の `check-quota.sh` は残す。
hook は stdin の JSON（session_id, cwd, transcript_path, hook_event_name …）と、環境変数 `ITERM_SESSION_ID`（`w0t1p0:<UUID>` の UUID 部分が iTerm2 API の session id）でペインに紐付ける。

| hook event | 遷移先 | 補足 |
|---|---|---|
| SessionStart | idle | agent_sessions を作り、ペインに紐付ける |
| UserPromptSubmit | run | last_prompt を更新する。title が空ならここで設定 |
| PreToolUse | run | current_tool = tool_name。`AskUserQuestion` のときは ask |
| PostToolUse | run | 許可待ちから戻ったこともここで拾う |
| Notification (`permission_prompt`) | ask | |
| Notification (`idle_prompt`) | done | |
| Stop | done | 1 ターン完了、つまりユーザーの返答待ち。last_message は daemon が transcript 末尾から後で抽出 |
| SessionEnd | ended | パネルの sessions からは消え、history に残る |

hook 側の制約

- 必ず exit 0 で終わる。例外は握りつぶしてログに書くだけ
- 1 回 50ms 以内を目標にする。transcript の解析のような重い処理は daemon 側で遅れて実行する
- `python -m saba_cli.hook` という軽量エントリにして、`cli.py` の import を通さない

### 4.2 Codex（補助経路）

- `~/.codex/config.toml` の `notify` は 1 プログラムしか登録できない。今は SkyComputerUseClient が入っているので、`saba-cli hook codex` で受けて処理したあと、元の notify をそのまま呼ぶ（chain 実行）。元のコマンドは設定ファイルに退避しておく
- notify で届くのは `agent-turn-complete`（done に相当）だけ。run の判定は下のフォールバックに頼る
- codex 0.164 が hooks に対応しているかは**未確認**。対応していれば Claude と同じ扱いにする

### 4.3 ペインの走査（daemon。hooks のフォールバックを兼ねる）

- daemon は全ウィンドウ・全タブ・全ペインを走査し、エージェントが動いているペインを session として出す。hooks を入れる前に起動した claude もここで拾い、状態は idle とする
- エージェントかどうかの判定は次の順に見る
  1. hooks で `ITERM_SESSION_ID` が分かっているペイン
  2. `jobPid` の実行ファイルのパス（`~/.local/share/claude/versions/<ver>` なら claude）
  3. `jobName` が `claude` / `codex`、またはバージョン文字列
  - **注意（spike で判明）**: Claude Code の `jobName` は `claude` ではなく `2.1.330` のようなバージョン文字列になる（実体が `~/.local/share/claude/versions/<ver>` のため）
- ペインのフォアグラウンドがエージェントでなくなったら ended にする
- 画面更新の有無で codex の run / idle を推定する。最後の更新から N 秒経ったら idle

### 4.4 iTerm2 本体への反映

- タブ色を状態で変える: run は青、ask は赤、done は橙、idle は灰（`LocalWriteOnlyProfile.set_tab_color`）。1 タブに session が複数あるときは、ask > done > run > idle の順で一番急ぐものの色にする。パネルを閉じていても状態が分かる
- done になったら macOS 通知を出す。設定で on/off できるようにし、そのペインを見ているときは出さない

## 5. タブと profile

### 5.1 CLI

```
saba-cli open [DIR] [--agent claude|codex|none] [--new]
    DIR（省略時は cwd）で 2 ペインのタブを開く。左は `claude --continue`（--new なら `claude`）、右はシェル。profile は作らない
saba-cli add [DIR] [--name N] [--group G] [--left CMD] [--right CMD] [--ratio N]
    profile を登録する（開かない）。既定は left=`claude --continue`、右ペインは DIR でシェル
saba-cli save [NAME] [--group G]
    今いるタブを profile として保存する（左右の cwd・コマンド・幅を取り込む）。同名があれば y/N で上書き
saba-cli profile ls [--json]
saba-cli profile open <NAME>    profile からタブを開く（押すたびに新しいタブ）
saba-cli profile rm <NAME>
saba-cli group add <NAME> [--color C] [--dir DIR]...   グループ作成（C: red|orange|green|cyan|blue|purple|gray）。--dir で dir ルールも付ける
saba-cli group rm <NAME>                グループ削除（中の profile・session は ungrouped に移るだけ）
saba-cli group set <PROFILE> <GROUP|->  profile の所属を変える（- で外す）
saba-cli group dir add|rm <GROUP> [DIR] dir ルールの追加・削除（DIR 省略時は cwd）。1 つの dir は 1 グループだけ
saba-cli group open <NAME>              グループの profile をまとめて開く
saba-cli group ls [--json]              グループ一覧（色・dir ルール・profile 数）
saba-cli ls [--json]            稼働中の session 一覧と状態
saba-cli history [-n N] [--cwd DIR] [--import] [--resume ID]
saba-cli panel start|stop|status|toggle|logs|doctor
saba-cli panel install|uninstall   venv と hooks の導入・撤去（§8）
saba-cli hook claude|codex      内部用。hooks から呼ばれる
```

既存の `--tree` / `--claude` フラグは残す。argparse は `add_subparsers(required=False)` を併用する形に広げ、後方互換を保つ。

### 5.2 タブ生成（`panel/layout.py`）

profile を開くときと `saba-cli open <dir>` で共通。

1. 対象ウィンドウ（今のキーウィンドウ。なければ新規作成）で `async_create_tab`
2. 左ペインで `cd <left_cwd> && <left_cmd>` を送る（cd に失敗したらコマンドは実行しない。fish / bash / zsh で同じ書き方が通る）
3. right があれば `async_split_pane(vertical=True)` で右ペインを作り、`cd <right_cwd> && <right_cmd>` を送る。幅を ratio に合わせる（方法は未確認。spike は 50:50 のまま）
4. 左ペインにフォーカスを戻し、タブ名を profile の name にする（`open <dir>` なら dir の basename）
5. profile の last_used を更新し、SSE で UI に通知する

- 同じ profile を何度押しても、そのたびに新しいタブを開く。開いているかどうかは見ない
- user var は設定しない。タブと profile の対応は持たない

CLI から daemon へは HTTP（127.0.0.1、token 付き）で依頼する。daemon がいないとき、`add / profile ls / profile rm / group` は DB を直接触るのでそのまま動く。`open` と `profile open` は osascript（タブ生成、split、write text）で最低限の動作を確保する。`save` はペインの情報が要るので daemon 必須にする。

### 5.3 session の検出

daemon の起動時と、`LayoutChangeMonitor` / `NewSessionMonitor` / `SessionTerminationMonitor` が発火したときに、§4.3 の走査をする。

- 並び順はウィンドウ → タブ → ペインの順（タブ順）。状態で並べ替えない
- 名前は cwd の basename。同じ名前が複数あれば、違いが出る親ディレクトリを前に付ける（`Acme-Unity_1/…/Codes`）。それでも同じ（同じ cwd の 2 ペインなど）なら ` ·2` を付ける
- hooks から来た session は agent_sessions に記録する。走査だけで見つけた session は表示するだけで、履歴には残さない

### 5.4 起動時の挙動

- iTerm2 を起動しても、タブの自動復元はしない。出すのはターミナル 1 つと右側のパネルだけ
  - パネルは自動では起動しない。ターミナルで `saba-cli panel start` を実行して出す（§8）
  - Toolbelt の表示状態は iTerm2 の設定に任せる。新しいウィンドウでも表示されるかは未確認
- 作業を始めるときは、パネルの profiles 欄から開きたいものを押す。押すたびにタブが 1 つできる
- `restore_on_launch` と `saba-cli restore` は作らない

### 5.5 tab profile

- profile は AI エージェントと無関係な「保存したタブを開くショートカット」。中身は name、group、左右ペインの cwd とコマンド、分割比だけ
- 使い方: `acme-1` / `acme-3` / `Journal` / `memo` のような profile を作っておく。iTerm2 の起動後にパネルで押していくと、タブが 1 つずつ開く。同じ profile を 2 回押せばタブが 2 つできる
- 保存は手動だけ。自動保存も自動復元もしない
- 保存の流れ
  1. 普通にタブを作る（cd して claude、右に split して cd）
  2. profiles 欄の `+ save` を押す。今フォーカスしているタブを取り込む。sessions の行を右クリック → `save tab as profile…` なら、その行のタブを取り込む
  3. `save>` 欄に取り込んだ内容（name / left / right / ratio / group）が入るので、確認・修正して `⏎`
  - CLI ならタブの中で `saba-cli save [NAME]`。一から登録するなら `saba-cli add <dir>`
- 取り込む内容（capture。iTerm2 API）
  - タブの 1 つ目のペインを左、2 つ目を右とし、それぞれの cwd（`path` 変数）を取る
  - コマンド: claude が動いていれば `claude --continue`、codex なら `codex resume --last`、シェルなら空。判定は §4.3 と同じ
  - 分割比: 左右の列数から概算する
- 扱うのは 1 ペインと左右 2 ペインだけ。3 ペイン以上のタブは先頭の 2 つだけを取り込み、その旨をメッセージに出す
- 既存の profile を直すときは `e`（右クリック → edit…）で `save>` 欄を開く
- グループの右クリック `open all` で、そのグループの profile をまとめて開ける

## 6. Panel UI

実物のモック: `docs/design/panel-mock.html`（ブラウザで開くとモックデータで動く。本実装はこのファイルを daemon が配信する）。細かい見た目はモックを正とする。

### 6.1 デザイン方針

- **ターミナルの延長に見せる**。AI ツールっぽい装飾は入れない
  - 全文字等幅フォント。角丸・影・グラデーション・絵文字・アイコンフォントは使わない
  - 枠は lazygit 風の 1px 罫線で、枠線の上にタイトルを載せる。ボタンは `[resume]` のような角括弧のテキスト
  - 配色は `--tree` と同じ Iceberg（theme.py のパレット）。ライトモードには Iceberg light を当てる
- **状態はグリフと色で一目で分かるようにする**。行の並び順は勝手に変えない（クリックしようとした行が動いて押し間違えるのを防ぐ）
- **マウスだけで完結する**。キーボード操作はおまけ扱いで、フッターに常にキーのヒントを出す
- **壊れても止まらない**。エラーは赤枠の `! error` ボックスに出し、最後に取れた状態は表示したまま残す

### 6.2 画面構成（幅 270〜420px）

```
saba-cli panel                ● live
⠹ 2 run  ! 1 ask  ● 1 done  ○ 2 idle      ← クリックでその状態の session へ順に移動
┌ ▾ sessions ──────────────────────┐      今動いているエージェント（ペイン単位・自動）
│ ▾ work ──────────────────────  2 │      グループ見出し（profiles と共通のグループ）
│ │▌⠹ saba_cli    claude   4m12s   │      ▌ = 今フォーカス中のペイン
│ │    main* · Edit docs/design/…  │      branch(*=dirty) · 実行中ツール
│ │    › 右側のパネルを terminal…   │      直近の prompt
│ │ !  Unity_1/…/Codes claude 0m42s│      赤: 許可待ち
│ │    feature/… · needs approval …│
│ └                                │
│ ▸ notes  ● 1 ────────────────  1 │      畳むと見出しに状態の件数を出す（ask を見落とさない）
│   ungrouped ─────────────────  1 │      session が 0 件のグループは出さない
│ │ ○  dotfiles   claude   1h      │
│ └                                │
└──────────────────────────────────┘
┌ ▾ profiles ───── + save  + group ┐      保存したタブ。押すたびに新しいタブを開く
│ ▾ work ──────────────────────  2 │      グループ見出し（名前がグループ色）
│ │ ▸ acme-1       ~/wkspaces/…    │      ▸ name  左ペインの dir
│ │ ▸ acme-3       ~/wkspaces/…    │
│ └                                │      縦線と └ でグループの範囲を示す
│ ▾ notes ─────────────────────  2 │
│ │ ▸ Journal      ~/wkspaces/…    │
│ │ ▸ memo         ~/wkspaces/memo │
│ └                                │
└──────────────────────────────────┘
┌ ▾ history ───────── [all] / ____ ┐
│ today                            │
│ 14:02 saba_cli   ⠹ iTerm2 統合…  │      稼働中は今の状態グリフを出す
│ 11:30 dotfiles   Notification…   │      クリックで詳細を開く → [resume]
│ yesterday                        │
└──────────────────────────────────┘
───────────────────────────────────
opened acme-1                              ← メッセージ行（4 秒で消える）
↑↓ move  ⏎ focus/open  s save  / find  ? keys
```

### 6.3 状態表示

sessions の行

| 状態 | グリフ | 色 | 2 行目 | 時間表示 |
|---|---|---|---|---|
| run（実行中） | braille スピナー | 青 | `ツール名 引数` か `thinking…` | 経過 `4m12s` |
| ask（許可待ち・AskUserQuestion） | `!` | 赤 | `needs approval` + コマンド | 待ち時間 `0m42s` |
| done（ターン完了、未確認） | `●` | 黄 | `your turn` + 最終メッセージ | `3m` |
| idle（確認済み・未入力） | `○` | 灰 | 最終メッセージ | `1h` |

- done は未読の扱いにする。そのペインにフォーカスした時点で idle に落とす。こうすると、黄色のものだけが「まだ見ていない完了」になる
- エージェントが終わった session は sessions から消え、history にだけ残る
- prefers-reduced-motion が有効なら、スピナーは止めて `●` を表示する

profiles の行は状態を持たない。`▸ name  dir  最終使用` の 1 行で、薄い色で出す。

### 6.4 操作

| 対象 | 操作 | 結果 |
|---|---|---|
| sessions の行 | クリック / `⏎` | そのペインにフォーカス（タブとウィンドウも前に出す） |
| 〃 | 右クリック | focus, history（この cwd に絞る）, move to group…, save tab as profile…, copy path, reveal in Finder |
| 〃 | 見出しへドラッグ / `g` / 右クリック → move to group… | 手動移動。その session の cwd をグループに記憶し、以後同じ cwd の session も同じグループに入る（§6.5） |
| 〃 | 右クリック → `auto (dir rules)` | 手動の記憶を消して dir ルールに戻す。手動で移した行（`group_src = manual`）にだけ出す |
| profiles の行 | クリック / `⏎` | 新しいタブを開く（押すたびに 1 つ） |
| 〃 | 右クリック | open, edit…（`save>` 欄）, rename, move to group…, move up / down, copy path, delete |
| 行の複数選択 | Shift+クリック / Ctrl+クリック / Shift+↑↓ / `Esc` | 範囲選択 / 1 件ずつトグル（⌘+クリックも同じ扱い）/ 範囲を広げる / 解除。選択は同じ欄の中だけ（sessions 同士、profiles 同士）。macOS の WebKit は Ctrl+クリックを `contextmenu` として出すので、`ctrlKey` 付きの `contextmenu` はメニューでなくトグル選択として扱う。本物の右クリックは従来どおりメニュー |
| 複数選択中 | 見出しへドラッグ / `g` / 右クリック → move to group… | 選択した行を全部同じグループへ移す |
| 〃 | `G` / 右クリック → new group… | `group>` 欄で新しいグループを作り、選択した行を全部そこへ移す。sessions のときは、選択した session の cwd を dirs の初期値に入れ、各 session も手動で移す |
| 複数選択中の profiles | `⏎` / `d` | 全部開く / まとめて削除（`y/N` の確認あり） |
| `+ save` / `s` | クリック | フォーカス中のタブを取り込んで `save>` 欄を開く（§5.5） |
| ヘッダのカウンタ | クリック | その状態の session を順番にフォーカスする（ask が 2 つあれば交互） |
| history の行 | クリック | 詳細を展開: 最初の prompt / 最後の返答 / branch / 所要時間 / `[resume]` `[copy id]` |
| history の `[all]` | クリック | 全件と、選択中の session の cwd だけ、を切り替える |
| profile / group の delete | — | 実行前に、メッセージ行で `y/N` の確認を出す（ブラウザのダイアログは使わない。Toolbelt の WebView が固まるため） |

### 6.5 グループ

- グループは sessions と profiles で共通。名前と色は両方の欄で同じものを使う。畳む状態は欄ごとに別に持つ（`collapsed` / `collapsed_sessions`）
- session の所属は表示のたびに cwd から決める。上から順に優先する
  1. **手動**: 行を移すと、その cwd を記憶する（`session_group_overrides`。cwd 完全一致）。`ungroup` で移すと「明示的に ungrouped」として記憶し、dir ルールより優先する
  2. **dir ルール**: グループの `dirs` に cwd と同じ dir か親 dir があれば入る。複数一致したら最長一致。例: `~/wkspaces` を work、`~/wkspaces/Acme-Unity_1` を proj にすると、Unity_1 の配下は proj、それ以外の wkspaces 配下は work
  3. どちらも無ければ ungrouped
- profile の所属は手動だけ。dir ルールは profile には効かない
- 0〜1 個のグループに所属する。所属なしは `ungrouped` の見出しにまとめる。グループが 1 つもなければ見出しは出さない
- sessions 欄では、session が 0 件のグループの見出しは出さない。並びはグループ順（`groups.sort`）で、グループの中はタブ順。ungrouped は最後
- sessions 欄のグループを畳むと、見出しに状態の件数（`! 1  ⠹ 2` など）を出す。畳んでいても ask と done を見落とさないようにするため
- 見出しの `▾` から縦線を下ろし、最後の行の下を `└` で閉じる。グループの範囲がひと目で分かるようにする（tree 表示と同じ読み方）
- グループ色は Iceberg パレットの 7 色（red / orange / green / cyan / blue / purple / gray）から選ぶ。DB には色の名前で保存し、描画は CSS 変数経由にする。ライトモードでも自動で読みやすい色になる
- グループ色を使うのは、見出しのグループ名と範囲を示す縦線だけ。状態グリフの色（青=run、赤=ask、黄=done）とは使う場所を分けて、意味が混ざらないようにする

| 操作 | 結果 |
|---|---|
| `+ group` / `G` | インラインの `group>` 欄が開く。名前を入れ、色は `tab` かクリックで選ぶ。入力中の文字がその色で表示されるので、そのままプレビューになる。`dirs` 行で dir ルールを入れる（補完つき・複数可・空でもよい）。他のグループと同じ dir はエラーになり、欄の中に理由を出す |
| 見出しをクリック | 折りたたみ / 展開（欄ごとに別） |
| 見出しの hover で出る `+` | profiles 欄だけ。そのグループを選んだ状態で `save>` 欄を開く |
| 見出しを右クリック | open all（profiles 欄だけ）/ collapse / rename・color・dirs / move up・down / delete（中の profile・session は ungrouped に移るだけ。y/N の確認あり） |
| 行を見出しにドラッグ | グループ移動。session は手動移動として cwd を記憶する |
| 行を右クリック → move to group… / `g` | メニューがグループ一覧に切り替わる。`ungroup` と `new group…`（作ってすぐそこに移す）も選べる。手動で移した session には `auto (dir rules)` も出る |
| `save>` 欄の `group` 行 | 保存するときに所属グループを選ぶ |

### 6.6 API 契約（UI ↔ daemon）

snapshot（`GET /api/state` は全量を返す。`GET /api/events` は SSE で、`state` イベントで全量を push する。規模が小さいので差分にはしない）

```
{
  active: sessionId|null,              // フォーカス中のペインが session ならその id
  fault: {title, detail, hint}|null,   // daemon が iTerm2 API で失敗した時
  sessions: [{id, tab, name, cwd, agent, branch, dirty, state, since, tool, arg, prompt, message, group, group_src}],
     // id = iTerm2 session id、tab = tab id。state: run|ask|done|idle。branch/dirty/since/tool/arg/prompt は null 可
     // group = gid|null、group_src = 'manual'|'dir'|null（daemon が §6.5 の規則で決める。明示 ungrouped は group=null, group_src='manual'）
  groups: [{id, name, color, collapsed, collapsed_sessions, dirs}],   // 表示順。dirs は `~` 形式・末尾 `/` なし
  profiles: [{id, name, group, left:{cwd, cmd}, right:{cwd, cmd}|null, ratio, last_used}],   // 表示順。last_used は null 可
  history: [{id, cwd, name, agent, started, ended, title, first, last, branch, live}]
}
```

`POST /api/action {op, ...}` は `{msg}` か `{error}` を返す。

| op | 引数 | 内容 |
|---|---|---|
| `focus` | `id`（session id） | そのペインへ移動 |
| `session.move` | `id`（session id）, `group`（gid / null / `"auto"`） | gid と null はその session の cwd を手動で記憶する（null は明示 ungrouped）。`"auto"` は記憶を消して dir ルールに戻す |
| `open` | `id`（profile id） | 新しいタブを開く（常に新規） |
| `capture` | `from`（session id か null） | そのタブ（null ならフォーカス中のタブ）を取り込み、`{msg, profile:{name, left, right, ratio}}` を返す。保存はしない |
| `save` | `id`（null で新規）, `name`, `group`, `left`, `right`, `ratio` | profile の作成・更新。同名があればエラー |
| `profile.delete` | `id` | |
| `profile.move` | `id`, `group`（null で ungrouped） | 移動先では末尾に置く |
| `profile.update` | `id`, `dir`（-1 / 1） | 同じグループの中で並べ替える |
| `group.create` | `name`, `color`, `dirs?`, `move?`（profile id）, `move_session?`（session id） | 作ってすぐそこに移すこともできる。dirs は空を除き、末尾 `/` を落とし、重複を除く。他のグループにある dir はエラー |
| `group.update` | `gid`, `name?`, `color?`, `collapsed?`, `collapsed_sessions?`, `dir?`, `dirs?` | `dirs` は丸ごと置き換え（検証は create と同じ） |
| `group.delete` | `gid` | 中の profile は ungrouped になる。そのグループを指す session の手動の記憶も消す |
| `group.open` | `gid` | グループの profile を順に開く |
| `resume` | `hid` | history の session を再開する |

- `GET /api/complete?p=` はディレクトリ補完。`[{path, note, registered}]` を返す。入力の親ディレクトリにあるディレクトリを最大 20 件、`~` を展開する。隠しディレクトリは入力が `.` で始まるときだけ出す
- snapshot の形はモックの `mockSource()` が正本で、daemon はこれに合わせる
- 複数選択の操作に専用の op は作らない。UI が既存の op（`session.move` / `profile.move` / `profile.delete` / `open` / `group.create`）を順に呼ぶ

状態の更新経路は hook → UDP ping → daemon → SSE。300ms 未満を目標にする。

## 7. 履歴

- 普段の記録: hooks で agent_sessions に書く。ended になった行も消さずに残す
- 初回の取り込み: `saba-cli history --import` で `~/.claude/projects/*/*.jsonl` と `~/.codex/sessions/**` を走査し、cwd・開始時刻・最初の prompt を取り込む。transcript 本体は複製せず、パスだけ持つ
- 表示: 日付別。cwd で絞り込める（session の右クリック → history）。title と last_prompt にテキスト検索をかけられる
- 再開: 稼働中ならそのペインへ移動する。終わっていれば、その cwd で新しいタブを開いて `claude --resume <id>` / `codex resume <id>` を実行する。履歴は profile とは関係しない

## 8. インストール・起動

起動は手動にする。iTerm2 の AutoLaunch は使わない。

```
saba-cli panel start    daemon をバックグラウンドで起動（pid と port は panel.json）。起動済みなら何もしない
saba-cli panel stop     daemon を止める（Toolbelt の中身は表示できなくなる）
saba-cli panel status   起動状態、port、iTerm2 との接続、直近のエラー
```

- iTerm2 の外から起動したプロセスは、`iterm2` パッケージが AppleScript で接続 cookie を取りに行く。spike ではターミナルから起動して接続できた
- 自動起動が欲しくなったら、`panel install --autolaunch` で AutoLaunch に起動用のシムを置く形で後から足せる

`saba-cli panel install` が行うこと:

1. iTerm2 Python API が有効か確認する。無効なら有効化の手順を案内する。設定は自動では変えない
2. venv `~/.local/share/saba-cli/venv` を作り、バージョンを固定した `iterm2` と saba-cli（editable）を入れる。saba-cli 本体の `pyproject.toml` の dependencies には何も足さない
3. Claude hooks を `~/.claude/settings.json` にマージする。既存の hook は残し、変更前にバックアップを取る
4. Codex の notify を chain 形式に差し替え、元のコマンドを退避する
5. 初回だけ、Toolbelt メニューで「saba-cli」を有効にする手順を表示する。iTerm2 はツールを表示名で覚えるので、名前を変えたらチェックし直しが要る

`saba-cli panel doctor` は次を確認する: API が有効か / daemon が動いているか / hooks が登録されているか / 最後にイベントを受け取った時刻。

## 9. 障害耐性（iTerm2 の仕様が変わっても止まらない）

方針: iTerm2 側で何かが壊れても、壊れた機能だけを止めてエラーを表示し、他の機能は動かし続ける。saba-cli 本体と Claude Code の動作は絶対に巻き込まない。

| 層 | 壊れ方の例 | 振る舞い |
|---|---|---|
| iTerm2 API 呼び出し | メソッド名の変更、例外、タイムアウト | API 呼び出しはすべて `panel/iterm_adapter.py` の 1 か所を通す。各呼び出しを try + timeout(3s) で包み、失敗したら `fault` として snapshot に載せる。daemon は落ちない |
| 機能単位 | タブ色の設定だけ失敗するなど | 起動時に capability probe（webview / monitor / tab color / activate / create tab / split）を実行する。失敗した機能だけ無効化して、パネルに表示する |
| ペイン単位 | 1 ペインだけ変数が読めない | そのペインだけ飛ばして一覧は出す。fault に 1 件目のエラーを載せる |
| iTerm2 との接続 | API server が無効、接続切れ | 指数バックオフ（1s から最大 30s）で再接続し続ける。その間も HTTP サーバと DB は動いているので、パネルは最後の状態を出したまま |
| Toolbelt 登録 | WebView tool の API がなくなった | `saba-cli panel url` でブラウザ表示に逃がす（同じ HTML がそのまま動く） |
| daemon 自体 | 想定外の例外 | タスクごとに例外を捕まえてログに出す。トップレベルは supervisor ループで再起動する |
| hook | DB ロック、想定外の payload | 必ず exit 0 で終わる。stderr には何も出さない（Claude の画面を汚さない） |
| CLI (`open` など) | daemon がいない | 1 行のエラーと `saba-cli panel doctor` の案内を出して exit 1 にする。traceback は出さない |
| UI | SSE 切断、不正な JSON | 赤枠の `! error` ボックスと `● error` を出し、最後に受け取った状態のまま操作できるようにする |

`iterm2` パッケージのバージョンは venv で固定する。`panel doctor` で iTerm2 本体と `iterm2` パッケージのバージョンを表示し、仕様変更が原因かどうか切り分けやすくする。

## 10. セキュリティ

- HTTP は 127.0.0.1 にだけ bind する。起動ごとにランダムな token を作り、`panel.json`（パーミッション 0600）に置く。WebView の URL に token を付け、API には token を必須にして、状態を変える API は Origin も確認する（ブラウザからの CSRF 対策）
- profile のコマンドはペインに送って実行されるので、`save` は token 必須の API からしか受け付けない。実行するのは DB の profile に保存されたコマンドと、`open <dir>` / `resume` の固定コマンドだけ
- prompt の内容はローカルの DB にだけ保存し、200 文字に切り詰める

## 11. モジュール構成

```
src/saba_cli/
  cli.py                 subparsers を追加（既存フラグとの互換を保つ）
  hook.py                軽量な hook エントリ（stdlib のみ）
  store/
    db.py                スキーマとマイグレーション
    models.py            dataclass（Profile, Group, AgentSession）と resolve_group（session の所属を cwd から決める純粋関数）
    paths.py
  agents/
    base.py claude.py codex.py   hook→状態遷移、transcript 解析、resume コマンド
  commands/
    open.py              open <dir>
    profile.py           add / save / profile / group
    sessions.py          ls
    history.py
    panel.py             start/stop/status/install/doctor/toggle/...
  panel/                 iterm2 に依存するのはここだけ
    __main__.py          daemon エントリ
    daemon.py            接続、monitor、supervisor
    iterm_adapter.py     iTerm2 API を呼ぶ唯一の場所（timeout + fault 化 + capability probe）
    sessions.py          ペインの走査、エージェント判定、名前の区別（§5.3）
    layout.py            タブ生成（profile / open）、capture、activate、タブ色
    server.py            asyncio HTTP + SSE（stdlib）
    static/index.html    docs/design/panel-mock.html をそのまま移す（単一ファイル、ビルドなし）
```

## 12. フェーズ

| Phase | 内容 | 完了条件 |
|---|---|---|
| 0 spike | `spike/toolbelt.py`。Toolbelt 表示、ペインの走査、クリックで移動、profile の capture / save / open、グループ | §13 の未確認項目を潰す |
| 1 | store（profiles / groups / group_dirs / session_group_overrides）、`open / add / save / profile / group / ls`、daemon、パネルの sessions（走査のみ・idle 固定）と profiles | profile を押すとタブが開き、session をクリックで移動できる |
| 2 | Claude hooks → 状態表示、件数サマリ、タブ色、通知 | 5 session 同時でも状態が正しく出る |
| 3 | 履歴の記録・import・検索・resume | 過去のセッションをパネルから再開できる |
| 4 | codex 対応、doctor、Status bar component（任意） | |

## 13. 未確認事項・リスク

確認済み（2026-11-23 spike、iTerm2 3.7.7 / iterm2 2.30 / websockets 17.4 / Python 3.14）

- `async_register_web_view_tool` で Toolbelt にパネルが表示され、パネルの操作が localhost API に届く（ユーザーの操作で確認）
- メニュー項目 `Show Toolbelt` は存在する（API から実行して切り替わるかは未確認）
- `path` 変数は shell integration なしでも正しい cwd を返す
- `jobName` は Claude Code だとバージョン文字列になる（§4.3 の注意）
- AutoLaunch を使わず、ターミナルから起動したプロセスでも接続できる
- iTerm2 は Toolbelt のツールを表示名で覚える（設定 `ToolbeltTools`）。名前を変えるとチェックが外れる

未確認

- session のクリック → `async_activate` でペインへ移動したあと、キーボードフォーカスがターミナルに移るか（Toolbelt の WebView に残らないか）
- `async_create_tab` 直後に送った `cd … && cmd` が、シェルの起動前でも取りこぼされないか
- split の幅を ratio に合わせる方法（`async_update_layout` / preferred size）
- ウィンドウごとに WebView が別々に作られるか
- Toolbelt を表示した状態が、新しいウィンドウにも引き継がれるか
- hook の中で `ITERM_SESSION_ID` が読めるか
- codex に hooks があるか
- Claude Code 2.1.330 での hook イベント名と、Notification の `notification_type`
- 複数選択の一括操作は、UI が既存 op を 1 件ずつ呼ぶ。件数が多くて遅い、または途中で失敗して中途半端になるのが問題になったら、Phase 1 で一括 op（`bulk`）を足す

## 14. 決定事項

| 項目 | 決定 |
|---|---|
| sessions と profiles | 分離する。sessions は稼働中のエージェントをペイン単位で自動表示、profiles はエージェントと無関係なタブのショートカット（§0） |
| profile を押したとき | 押すたびに新しいタブを開く。既に開いているかは見ない（§5.2） |
| グループ | sessions と profiles で共通。sessions は dir ルール（最長一致）＋手動（cwd 単位で記憶、手動が優先）、profiles は手動だけ（§6.5） |
| 複数選択 | Shift+クリックで範囲、Ctrl+クリック（⌘+クリックも可）で 1 件ずつトグル、Shift+↑↓ で範囲拡張、Esc で解除。選択は同じ欄の中だけ。まとめてグループ移動・新規グループ作成ができ、profiles は全部開く・まとめて削除もできる。一括 op は作らず既存 op を順に呼ぶ（§6.4） |
| 起動時 | 復元しない。ターミナル 1 つとパネルだけ出す（§5.4） |
| パネルの起動 | 手動（`saba-cli panel start`）。AutoLaunch は使わない（§8） |
| `open <dir>` の左ペイン | `claude --continue`。新規セッションは `--new` |
| profile のコマンド | 保存したものをそのまま実行する。取り込み時の既定は、claude なら `claude --continue`、codex なら `codex resume --last`、シェルなら空 |
| `iterm2` 依存 | saba-cli 本体には入れない。専用 venv に隔離し、壊れたらエラーを表示して動き続ける（§9） |
| UI | ターミナル調。AI ツールっぽい装飾はしない（§6.1） |
| tab profile の保存 | 手動だけ（`+ save` / `saba-cli save` / `saba-cli add`）。自動保存・自動復元はしない（§5.5） |
