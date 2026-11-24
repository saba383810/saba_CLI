"""Phase 0 spike: パネルを iTerm2 の Toolbelt に出し、sessions と profiles を実データで試す。

使い方:
  ~/.local/share/saba-cli/venv/bin/python spike/toolbelt.py          # 実データ
  ~/.local/share/saba-cli/venv/bin/python spike/toolbelt.py --mock   # モックデータ（UI 確認用）
  View > Show Toolbelt (⌘⇧B) → メニュー Toolbelt で "saba-cli" にチェック

実データモードでできること:
  sessions（自動検出。登録不要）
    - 全ウィンドウ・全タブ・全ペインを走査し、AI agent（claude / codex）が動いているペインを 1 行ずつ出す
      1 タブに 2 つあれば 2 行。並びはタブ順
    - 状態（run / ask / done / idle）は hooks を使わず、agent ペインの画面下部を読んで推定する（read_screen）
      Claude Code / Codex の表示が変わると外れる。正確さが要るなら Phase 1 で hooks に切り替える
    - 行をクリック → そのペインへ移動
    - グループ: 手動で移すと cwd 単位で記憶（最優先）→ グループの dir ルール（最長一致）→ ungrouped
      "auto" に戻すと手動の記憶を消して dir ルールに従う
  profiles（手動保存。STORE_PATH の JSON。AI agent とは無関係な「タブを開くショートカット」）
    - capture: フォーカス中のタブ（または指定した session のタブ）の左右の cwd・コマンド・分割比を取り込む
    - save / 削除 / グループ移動 / グループ内の並べ替え
    - 行をクリック → 押すたびに新しいタブを開く（左右で cd してコマンドを実行）。分割比は spike では未対応（50:50）
    - グループの所属は手動だけ（dir ルールは効かない）
  groups（sessions と profiles で共通）
    - 作成・変更・並べ替え・削除・まとめて開く。dir ルール（dirs）の編集。畳む状態は欄ごとに別
  /api/complete: ディレクトリ補完
  resume などほかの操作は spike では未対応で、エラーを返す
Ctrl-C で終了。終了すると Toolbelt の中身は表示できなくなる。
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import json
import os
import pathlib
import re
import secrets
import shlex
import threading
import time
import urllib.parse

import iterm2

DESIGN_DIR = pathlib.Path(__file__).resolve().parents[1] / "docs" / "design"
TOOL_ID = "dev.saba.panel.spike"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+")  # Claude Code の jobName は "2.1.330" のようなバージョン文字列になる
TOKEN = secrets.token_urlsafe(16)
STORE_PATH = pathlib.Path.home() / ".local" / "state" / "saba-cli" / "spike-store.json"
GCOLORS = ("purple", "cyan", "green", "blue", "orange", "red", "gray")
RESUME_CMD = {"claude": "claude --continue", "codex": "codex resume --last"}


class Hub:
    """asyncio 側で作ったスナップショットを HTTP スレッドへ渡す。"""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.snap = ""
        self.ver = 0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.conn: iterm2.Connection | None = None

    def publish(self, snap: str) -> None:
        with self.cond:
            if snap != self.snap:
                self.snap, self.ver = snap, self.ver + 1
                self.cond.notify_all()

    def wait(self, ver: int, timeout: float) -> tuple[int, str]:
        with self.cond:
            self.cond.wait_for(lambda: self.ver != ver, timeout)
            return self.ver, self.snap

    def call(self, coro, timeout: float = 5.0):
        """HTTP スレッドから iTerm2 API を呼ぶ。固まっても timeout で返す。"""
        return asyncio.run_coroutine_threadsafe(asyncio.wait_for(coro, timeout), self.loop).result(timeout + 1)


HUB = Hub()


class Store:
    """groups / profiles / セッションの手動グループの保存先（spike 用の JSON）。
    asyncio 側からしか触らないのでロックは要らない。

    profiles の並び順はリストの順そのもの。グループごとの表示はこの順を保ったまま絞り込む。
    session_groups は {cwd: gid}。"" は「明示的に ungrouped」（dir ルールより優先）。
    """

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        try:
            d = json.loads(path.read_text())
        except (OSError, ValueError):
            d = {}
        # 古い store（dirs / collapsed_sessions が無い）も読めるよう既定値で補う
        self.groups: list[dict] = [{**GROUP_DEFAULTS, **g, "dirs": list(g.get("dirs") or [])}
                                   for g in d.get("groups", [])]
        self.profiles: list[dict] = d.get("profiles", [])
        self.session_groups: dict[str, str] = dict(d.get("session_groups") or {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        body = {"groups": self.groups, "profiles": self.profiles, "session_groups": self.session_groups}
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1))
        tmp.replace(self.path)

    def group(self, gid: str | None) -> dict:
        g = next((g for g in self.groups if g["id"] == gid), None)
        if g is None:
            raise LookupError(f"group {gid} is gone")
        return g

    def profile(self, pid: str | None) -> dict:
        p = next((p for p in self.profiles if p["id"] == pid), None)
        if p is None:
            raise LookupError(f"profile {pid} is gone")
        return p

    def check_group_name(self, name: str, skip: dict | None = None) -> None:
        if any(g is not skip and g["name"] == name for g in self.groups):
            raise ValueError(f'group "{name}" already exists')

    def check_profile_name(self, name: str, skip: dict | None = None) -> None:
        if any(p is not skip and p["name"] == name for p in self.profiles):
            raise ValueError(f'profile "{name}" already exists')

    def group_or_none(self, gid: str | None) -> str | None:
        return self.group(gid)["id"] if gid else None

    def check_dirs(self, dirs: list[str], skip: dict | None = None) -> None:
        """dir ルールは 1 つの dir につき 1 グループだけ。"""
        mine = {expand_dir(d) for d in dirs}
        for g in self.groups:
            if g is skip:
                continue
            for d in g.get("dirs", []):
                if expand_dir(d) in mine:
                    raise ValueError(f"{tilde(expand_dir(d))} is already in group {g['name']}")

    def move_session(self, cwd: str, group: str | None) -> str:
        """セッションの所属を cwd 単位で記憶する。group は gid / None（明示 ungrouped）/ "auto"（記憶を消す）。"""
        key = cwd_key(cwd)
        if not key:
            raise ValueError("this session has no dir")
        name = os.path.basename(key) or key
        if group == "auto":
            self.session_groups.pop(key, None)
            gid, _ = resolve_group(key, self.groups, self.session_groups)
            dest = self.group(gid)["name"] if gid else "ungrouped"
            return f"{name} follows dir rules again → {dest}"
        if group:
            g = self.group(group)
            self.session_groups[key] = g["id"]
            dest = g["name"]
        else:
            self.session_groups[key] = ""
            dest = "ungrouped"
        return f"moved {name} → {dest} · remembered for {tilde(key)}"


GROUP_DEFAULTS = {"collapsed": False, "collapsed_sessions": False}
STORE = Store(STORE_PATH)


# ---------- 純粋関数（iTerm2 に繋がずにテストできる） ----------

def agent_of(job: str) -> str:
    if job in ("claude", "codex"):
        return job
    if VERSION_RE.match(job or ""):
        return "claude"
    return "none"


def cmd_for(job: str) -> str:
    """capture 時に左右ペインのコマンドを推定する。agent なら続きから再開するコマンド、シェルなら空。"""
    return RESUME_CMD.get(agent_of(job), "")


def ratio_of(left_cols: int, right_cols: int) -> int:
    total = left_cols + right_cols
    return round(left_cols * 100 / total) if total > 0 else 50


def expand_dir(d: str) -> str:
    """dir ルールの比較用。~ を展開し、末尾の / を落とす（"/" だけはそのまま）。"""
    return os.path.expanduser(str(d).strip()).rstrip("/") or "/"


def cwd_key(cwd: str) -> str:
    """手動グループの記憶に使うキー。空の cwd は ""。"""
    cwd = str(cwd or "").strip()
    return (cwd.rstrip("/") or "/") if cwd else ""


def tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def normalize_dirs(dirs) -> list[str]:
    """dir ルールの入力を揃える。空を除き、末尾の / を落とし、重複を除く。ホーム配下は ~ 形式で保存する。"""
    if dirs is None:
        return []
    if isinstance(dirs, str):
        dirs = [dirs]
    if not isinstance(dirs, list):
        raise ValueError("dirs must be a list of paths")
    out: list[str] = []
    seen: set[str] = set()
    for d in dirs:
        if not str(d or "").strip():
            continue
        full = expand_dir(d)
        if not full.startswith("/"):
            raise ValueError(f"{str(d).strip()} must be an absolute path or start with ~")
        if full not in seen:
            seen.add(full)
            out.append(tilde(full))
    return out


def resolve_group(cwd: str, groups: list[dict], overrides: dict[str, str]) -> tuple[str | None, str | None]:
    """セッションの所属グループと、その理由（'manual' / 'dir' / None）を返す。

    1. 手動の記憶（cwd 完全一致）。"" は明示的に ungrouped。存在しない gid は無視して 2 へ
    2. グループの dir ルール。cwd がその dir と同じか配下なら一致。複数一致は最長一致
    3. どちらも無ければ ungrouped
    """
    key = cwd_key(cwd)
    if not key:
        return None, None
    gids = {g["id"] for g in groups}
    if key in overrides:
        v = overrides[key]
        if v == "":
            return None, "manual"
        if v in gids:
            return v, "manual"
    best, best_len = None, -1
    for g in groups:
        for d in g.get("dirs", []):
            e = expand_dir(d)
            prefix = e if e.endswith("/") else e + "/"
            if (key == e or key.startswith(prefix)) and len(e) > best_len:
                best, best_len = g["id"], len(e)
    return (best, "dir") if best else (None, None)


def _disambiguate(items: list[dict]) -> None:
    """同じ名前の行を区別する。

    1. cwd が違えば、違いが出る親ディレクトリを前に付ける（Acme-Unity_1/…/Codes）
    2. それでも同じ名前（同じ cwd の 2 ペインなど）なら、2 つ目以降に " ·2" " ·3" を付ける
    """
    by: dict[str, list[dict]] = {}
    for it in items:
        by.setdefault(it["name"], []).append(it)
    for name, same in by.items():
        if len(same) < 2:
            continue
        parts = [it["cwd"].rstrip("/").split("/") for it in same]
        i = next((k for k in range(min(map(len, parts))) if len({p[k] for p in parts}) > 1), None)
        if i is None:
            continue
        for it, p in zip(same, parts):
            it["name"] = f"{p[i]}/{name}" if i == len(p) - 2 else f"{p[i]}/…/{name}"
    seen: dict[str, int] = {}
    for it in items:
        n = seen[it["name"]] = seen.get(it["name"], 0) + 1
        if n > 1:
            it["name"] = f"{it['name']} ·{n}"


def _pane(v, required: bool) -> dict | None:
    if v is None and not required:
        return None
    if not isinstance(v, dict) or not str(v.get("cwd") or "").strip():
        raise ValueError("pane needs a dir")
    return {"cwd": str(v["cwd"]).strip(), "cmd": str(v.get("cmd") or "").strip()}


def normalize_profile(a: dict) -> dict:
    """save の入力を検証して profile の形に揃える（id / group / last_used は呼び出し側で決める）。"""
    name = str(a.get("name") or "").strip()
    if not name:
        raise ValueError("type a name")
    try:
        ratio = int(a.get("ratio") or 50)
    except (TypeError, ValueError):
        raise ValueError("ratio must be a number") from None
    return {"name": name, "left": _pane(a.get("left"), True), "right": _pane(a.get("right"), False),
            "ratio": min(90, max(10, ratio))}


def shell_line(pane: dict) -> str:
    """ペインに送る 1 行。cd に失敗したらコマンドは実行しない（fish 3 / bash / zsh で同じ書き方が通る）。"""
    line = f"cd {shlex.quote(os.path.expanduser(pane['cwd']))}"
    if pane.get("cmd"):
        line += f" && {pane['cmd']}"
    return line + "\n"


def complete(p: str, known: set[str] | None = None, limit: int = 20) -> list[dict]:
    """ディレクトリ補完。入力の親ディレクトリにあるディレクトリを返す。隠しディレクトリは入力が . で始まる時だけ。"""
    known = known or set()
    p = p.strip() or "~/"
    full = os.path.expanduser(p)
    parent, prefix = (full, "") if p.endswith("/") else os.path.split(full)
    try:
        names = sorted(os.listdir(parent or "."))
    except OSError:
        return []
    home = os.path.expanduser("~")
    out = []
    for n in names:
        if not n.startswith(prefix) or (n.startswith(".") and not prefix.startswith(".")):
            continue
        path = os.path.join(parent, n)
        if not os.path.isdir(path):
            continue
        shown = "~" + path[len(home):] if p.startswith("~") and path.startswith(home) else path
        reg = path in known
        out.append({"path": shown, "note": "profile" if reg else "", "registered": reg})
        if len(out) >= limit:
            break
    return out


# ---------- 画面からの状態推定（純粋関数） ----------
# 文言は Claude Code 2.1.330 の実画面と Codex 0.164 のソース（codex-rs/tui）で確かめたもの。
# Claude Code の画面の下部はおおむね次の形:
#   ⏺ Bash(uloop compile)                           ← 直近のツール呼び出し（⏺ のあとが文章なら返答の本文）
#   ✻ Finagling… (4s · ↓ 395 tokens · …)          ← 実行中のスピナー（終わると「✻ Crunched for 5m 4s · done 15:36」）
#   ─────────────── [ラベル] ─                       ← 入力欄の上の罫線
#   ❯ 下書き                                         ← 入力欄
#   ───────────────
#     statusline / ⏵⏵ auto mode on · esc to interrupt ← 実行中は「esc to interrupt」が出る
# 承認・質問のダイアログが出ている間は入力欄が消える。

SPIN = "·✢✳✶✻✽"
_RULE = re.compile(r"^\s*─{8,}")
_OPTION = re.compile(r"^\s*(?:❯\s*)?\d+\.\s")             # 選択肢の行（❯ は今の選択）
_OPTION1 = re.compile(r"^\s*(?:❯\s*)?1\.\s")
_SPINNER = re.compile(rf"^[{SPIN}]\s+(?P<text>[^…]+?)…\s*(?:\((?P<info>[^)]*)\)?)?\s*$")
_FINISHED = re.compile(rf"^[{SPIN}]\s+\S+ for (?:\d+[hms]\s*)+"
                       r"(?:\s*·\s*done (?P<at>\d{1,2}:\d{2}))?(?:\s*·\s*(?P<bg>.+ still running))?\s*$")
_WAITING = re.compile(rf"^[{SPIN}]\s+Waiting for (?P<what>.+?) to finish\s*$")
_TOOL_CALL = re.compile(r"^⏺ (?P<tool>[A-Za-z_][\w.:-]*(?: - [\w.:-]+)?(?: \(MCP\))?)\((?P<arg>.*?)\)?\s*$")
_TOOL_GROUP = re.compile(r"^⏺ (?P<tool>Reading|Searching for|Running|Calling|Fetching|Writing|Editing|Listing|Waiting for)"
                         r" (?P<arg>.+?)…?\s*$")
_ELAPSED = re.compile(r"^(?:(?P<h>\d+)h\s*)?(?:(?P<m>\d+)m\s*)?(?:(?P<s>\d+)s)?$")
_CLAUDE_ASK_TITLES = {"Bash command": "Bash", "Run command": "Bash", "Edit file": "Edit", "Create file": "Write",
                      "Write file": "Write", "Read file": "Read", "Fetch": "WebFetch", "Notebook edit": "NotebookEdit"}
# まとめ表示（⏺ Reading 1 file… など）の動詞 → tool
_GROUP_TOOL = {"Reading": "Read", "Writing": "Write", "Editing": "Edit", "Running": "Bash", "Searching for": "Search",
               "Calling": "Call", "Fetching": "Fetch", "Listing": "List", "Waiting for": "waiting"}
_FILE_TOOLS = {"Read", "Write", "Edit", "Update", "Create", "MultiEdit", "NotebookEdit"}
# 読む・探す・書くだけのツールは compile などの「待ち」にしない
_QUIET_TOOLS = _FILE_TOOLS | {"Search", "List", "Fetch", "WebFetch", "WebSearch", "Grep", "Glob"}
# Codex の承認ダイアログの見出し → tool
_CODEX_ASK = (("Would you like to run the following command?", "shell"),
              ("Would you like to make the following edits?", "edit"),
              ("Would you like to grant these permissions?", "permissions"),
              ("Would you like to send input to", "stdin"),
              ("Do you want to approve network access to", "network"),
              ("needs your approval.", "mcp"),
              ("Submit with unanswered questions?", "question"))
# 実行中のツールの中身から「何を待っているか」を決める。上から順に最初に当たったもの
_ACTIVITY = (("compiling", re.compile(r"compil|\btsc\b|\bjavac\b|\bcsc\b", re.I)),
             ("testing", re.compile(r"run-tests|\btests?\b|pytest|jest|vitest|\bnunit|\bxunit|EditMode|PlayMode", re.I)),
             ("building", re.compile(r"\bbuild\b|msbuild|xcodebuild|gradle|BuildPlayer", re.I)),
             ("playing", re.compile(r"control-play-mode|replay-input|record-input|simulate-(?:keyboard|mouse)", re.I)),
             ("unity", re.compile(r"\buloop\b|\bunity\b|uLoopMCP", re.I)))
# 中身を見るだけのコマンドは「待ち」にしない（git commit -m "fix build" などの誤判定よけ）
_NOT_ACTIVITY = re.compile(r"^(?:git|gh|rg|grep|cat|echo|ls|sed|head|tail|find|less|wc|diff)\b")


def _clip(s: str | None, n: int = 200) -> str | None:
    s = " ".join((s or "").split())
    return (s[:n - 1] + "…" if len(s) > n else s) or None


def parse_elapsed(text: str | None) -> int | None:
    """「4s」「1m 23s」「1h 00m 05s」を秒に。読めなければ None。"""
    m = _ELAPSED.match((text or "").strip())
    if not m or not any(m.groups()):
        return None
    return int(m["h"] or 0) * 3600 + int(m["m"] or 0) * 60 + int(m["s"] or 0)


def clock_to_epoch(hhmm: str | None, now: float) -> float | None:
    """「done 16:20」の時刻を epoch に。未来なら前日とみなす。"""
    if not hhmm:
        return None
    h, m = map(int, hhmm.split(":"))
    lt = time.localtime(now)
    t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
    return t - 86400 if t > now + 60 else t


def activity_of(tool: str | None, arg: str | None, hint: str = "") -> str | None:
    """compile / build / test / Unity 待ちなら compiling / building / testing / playing / unity。

    hint は Bash の説明行（「⏺ Run EditMode tests」）。コマンドが長くて 1 行目に uloop 等が見えない時の手がかり。"""
    arg = (arg or "").strip()
    if tool in _QUIET_TOOLS or (tool in ("Bash", "shell") and _NOT_ACTIVITY.match(arg)):
        return None
    text = f"{tool or ''} {arg} {hint}"
    return next((label for label, rx in _ACTIVITY if rx.search(text)), None)


def _norm(lines: list[str]) -> list[str]:
    # iTerm2 はカーソル移動で飛ばしたセルを NUL で返す。Claude Code は「❯」「⎿」のあとに NBSP を置く。どちらも空白に
    return [str(x or "").replace("\x00", " ").replace("\xa0", " ").rstrip() for x in lines]


def _input_box(lines: list[str]) -> tuple[int, int] | None:
    """Claude Code の入力欄（罫線 → ❯ … → 罫線）の上下の罫線の位置。ダイアログ表示中は None。"""
    for i in range(len(lines) - 2, -1, -1):
        if _RULE.match(lines[i]) and lines[i + 1].startswith("❯") and not _OPTION.match(lines[i + 1]):
            end = next((j for j in range(i + 1, len(lines)) if _RULE.match(lines[j])), len(lines))
            return i, end
    return None


def _call(tool: str, arg: str) -> tuple[str, str | None]:
    arg = arg.strip()
    if tool in _FILE_TOOLS and "/" in arg and " " not in arg:
        arg = os.path.basename(arg.rstrip("/"))
    return tool, _clip(arg, 160)


def _tool_at(lines: list[str], i: int, end: int) -> tuple[str, str | None] | None:
    """lines[i]（⏺ の行）がツール呼び出しなら (tool, arg)。返答の文章なら None。"""
    nxt = next((x.strip() for x in lines[i + 1:min(end, i + 3)] if x.strip()), "")
    if nxt.startswith("⎿") and nxt[1:].strip().startswith("$ "):  # Bash は「⏺ 説明」+「⎿  $ コマンド」の 2 行
        return _call("Bash", nxt[1:].strip()[2:])
    m = _TOOL_CALL.match(lines[i])
    if m:
        return _call(m["tool"], m["arg"])
    m = _TOOL_GROUP.match(lines[i])  # 「⏺ Calling plugin:figma:figma 2 times · 2s…」の末尾の経過時間は落とす
    return _call(_GROUP_TOOL[m["tool"]], re.sub(r"\s*·\s*(?:\d+[hms]\s*)+$", "", m["arg"])) if m else None


def _last_call(lines: list[str], end: int, limit: int = 40) -> tuple[str | None, str | None, str]:
    """end より上の直近のツール呼び出し (tool, arg, Bash の説明行)。ユーザー入力（❯）を越えては探さない。
    ⏺ のあとが文章なら (None, None, "")（考え中）。説明行は activity_of の hint に使う。"""
    for i in range(end - 1, max(0, end - limit) - 1, -1):
        line = lines[i]
        if line.startswith("❯ "):
            break
        if not line.startswith("⏺ ") or " finished · " in line:  # 「⏺ Agent "…" finished · 18m 4s」は通知
            continue
        tool, arg = _tool_at(lines, i, end) or (None, None)
        return tool, arg, (lines[i][2:] if tool == "Bash" else "")
    return None, None, ""


def _last_prompt(lines: list[str], end: int) -> str | None:
    """画面に残っている最後のユーザー入力（入力欄より上の「❯ …」）。"""
    for line in reversed(lines[:end]):
        if line.startswith("❯ ") and not _OPTION.match(line) and line[2:].strip():
            return _clip(line[2:], 120)
    return None


def _summary(lines: list[str], end: int) -> str | None:
    """終わったターンの要約。「※ recap: …」があればそれ、無ければ最後の返答の書き出し（⏺ のあとが文章の行）。"""
    for i in range(end - 1, -1, -1):
        line = lines[i]
        if line.startswith("※ recap:"):
            more = lines[i + 1] if i + 1 < end and lines[i + 1].startswith("  ") else ""
            return _clip(line[len("※ recap:"):] + more, 160)
        if line.startswith("❯ "):
            break
        if line.startswith("⏺ ") and " finished · " not in line and _tool_at(lines, i, end) is None:
            return _clip(line[2:], 160)
    return None


def _claude_ask(lines: list[str]) -> dict | None:
    """入力欄が消えている時の承認・質問ダイアログ。"""
    # ダイアログは最後の会話の行（⏺ / ユーザー入力の ❯）より下の、最初の罫線から始まる。
    # 返答の本文にある「1. …」や「Do you want …?」を拾わないよう、そこから下だけを見る
    last = next((i for i in range(len(lines) - 1, -1, -1)
                 if lines[i].startswith("⏺ ") or (lines[i].startswith("❯ ") and not _OPTION.match(lines[i]))), -1)
    start = next((i for i in range(last + 1, len(lines)) if _RULE.match(lines[i])), last)
    q = next((i for i in range(len(lines) - 1, start, -1)
              if re.match(r"^\s*(?:Do you want to .+\?|.*Would you like to proceed\?)\s*$", lines[i])), None)
    opt = next((i for i in range(start + 1, len(lines)) if _OPTION1.match(lines[i])), None)
    # 「Enter to select」は選択式の質問（AskUserQuestion）の足元。プレビュー付きだと選択肢が画面外に出ることがある
    select = any("Enter to select" in x for x in lines[-6:])
    cancel = any("Esc to cancel" in x for x in lines[-6:])
    if q is None and not select and not (opt is not None and cancel):
        return None
    ask = {"state": "ask", "tool": None, "arg": None, "message": None}
    if q is not None and "Would you like to proceed?" in lines[q]:  # plan の承認
        head = next((i for i in range(q, -1, -1) if "Here is Claude's plan:" in lines[i]), None)
        first = next((x.strip(" │#") for x in lines[head + 1:q] if x.strip(" │╭╮─")), None) if head is not None else None
        ask.update(tool="plan", arg=_clip(first or "ready to code?", 120), message="Ready to code?")
        return ask
    if q is not None:  # ツールの承認: 罫線 → 見出し（Bash command など）→ 中身 → Do you want to …?
        top = next((i for i in range(q, start - 1, -1) if _RULE.match(lines[i])), start)  # 枠は上辺だけの罫線
        body = [x.strip() for x in lines[top + 1:q] if x.strip()]
        title = body[0] if body else ""
        content = body[1] if len(body) > 1 else ""
        if title == "Tool use":  # MCP: 「server - tool(args) (MCP)」
            m = re.match(r"^(?P<tool>.+?)\((?P<arg>.*?)\)\s*(?:\(MCP\))?$", content)
            tool, arg = (m["tool"].strip(), m["arg"]) if m else ("MCP", content)
        else:
            tool, arg = _CLAUDE_ASK_TITLES.get(title, title or "tool"), content
        ask["tool"], ask["arg"] = _call(tool, arg)
        ask["message"] = _clip(lines[q], 120)
        return ask
    # AskUserQuestion: 1 つ目の選択肢のすぐ上のひとかたまり（長いと「│ 」付きで折り返す）が質問文
    block: list[str] = []
    for x in reversed(lines[:opt] if opt is not None else []):
        s = x.strip()
        if not s or _RULE.match(x) or s.startswith("←"):  # ← はタブ（☐ ベース ☐ PR …）
            if block:
                break
            continue
        block.insert(0, s.lstrip("│").strip())
    question = "".join(b if not a or (a[-1] > "\x7f" and b[:1] > "\x7f") else " " + b
                       for a, b in zip([""] + block, block)) or None
    ask.update(tool="question", arg=_clip(question, 120), message=_clip(question, 160))
    return ask


def _read_claude(lines: list[str]) -> dict:
    out = {"state": "ready", "tool": None, "arg": None, "message": None, "prompt": None,
           "summary": None, "marker": None, "elapsed": None, "done_at": None}
    box = _input_box(lines)
    if box is None:
        ask = _claude_ask(lines)
        if ask:
            out.update(ask, prompt=_last_prompt(lines, len(lines)))
            return out
        top, below = len(lines), []
    else:
        top, below = box[0], lines[box[1] + 1:]
    out["prompt"] = _last_prompt(lines, top)
    # 入力欄のすぐ上（todo リストや通知を挟むことがある）にあるスピナー / 終了行を探す
    spin = None
    near = [i for i in range(top - 1, -1, -1) if lines[i].strip()][:16]
    for i in near:
        line = lines[i]
        if line.startswith("❯ "):
            break
        m = _SPINNER.match(line) or _FINISHED.match(line) or _WAITING.match(line)
        if m:
            spin = (i, m)
            break
    interrupt = any("esc to interrupt" in x for x in below)
    if spin and spin[1].re is _SPINNER or interrupt:
        out["state"] = "run"
        end = spin[0] if spin else top
        out["tool"], out["arg"], hint = _last_call(lines, end)
        if spin and spin[1].re is _SPINNER:
            text, info = spin[1]["text"], spin[1]["info"] or ""
            out["elapsed"] = parse_elapsed(re.split(r"[·•]", info)[0])
            if text.startswith("Compacting") and not out["tool"]:
                out["tool"] = "compacting"
        out["message"] = activity_of(out["tool"], out["arg"], hint)
        return out
    if spin:
        i, m = spin
        out["marker"] = m.group(0).strip()
        out["done_at"] = m.groupdict().get("at")
        bg = m["what"] if m.re is _WAITING else m["bg"]
        if bg:  # ターンは終わったが裏の作業（agent / shell）の完了で自動的に続く。待たされているのは agent 側
            out.update(state="run", tool="waiting", arg=_clip(bg, 80))
            out["message"] = activity_of(*_last_call(lines, i))
            return out
    out["summary"] = _summary(lines, top)
    return out


def _read_codex(lines: list[str]) -> dict:
    """Codex は実画面で未確認。codex-rs/tui のソースの文言で判定する。"""
    out = {"state": "ready", "tool": None, "arg": None, "message": None, "prompt": None,
           "summary": None, "marker": None, "elapsed": None, "done_at": None}
    tail = [x for x in lines if x.strip()][-30:]
    for i, line in enumerate(tail):
        hit = next((tool for key, tool in _CODEX_ASK if key in line), None)
        if hit:
            arg = next((x.strip()[2:] for x in tail[i + 1:] if x.strip().startswith("$ ")), None)
            if hit == "network":
                m = re.search(r'network access to "([^"]+)"', line)
                arg = m[1] if m else arg
            out.update(state="ask", tool=hit, arg=_clip(arg, 160), message=_clip(line, 120))
            return out
    if any(re.match(r"^\s*(?:› )?\d+\.\s", x) for x in tail) and any(re.search(r"Question \d+/\d+", x) for x in tail):
        q = next((x.strip() for x in tail if re.search(r"Question \d+/\d+", x)), None)
        out.update(state="ask", tool="question", arg=_clip(q, 120), message=_clip(q, 160))
        return out
    status = next((x for x in reversed(tail[-12:]) if "to interrupt)" in x), None)
    if status:
        out["state"] = "run"
        m = re.search(r"\((?P<el>[^•·()]+?)\s*[•·]\s*\S+ to interrupt\)", status)
        out["elapsed"] = parse_elapsed(m["el"]) if m else None
        run = next((x for x in reversed(tail) if re.match(r"^[•◦]\s+Running\s+\S", x)), None)
        if run:
            out["tool"], out["arg"] = "shell", _clip(re.sub(r"^[•◦]\s+Running\s+", "", run), 160)
        out["message"] = activity_of(out["tool"], out["arg"])
    return out


def read_screen(lines: list[str], agent: str) -> dict:
    """agent ペインの画面下部の行から、その瞬間の状態を読む（純粋関数）。

    state は run（作業中）/ ask（承認・質問待ち）/ ready（入力待ち）。done / idle の区別は StateTracker が付ける。
    tool / arg = 実行中（承認待ち）のツールと引数、message = run なら compiling などの待ち種別・ask なら質問文、
    prompt = 画面に残る最後のユーザー入力、summary = 終わったターンの要約、marker = ターン終了行（done 判定用）、
    elapsed = スピナーの経過秒、done_at = 終了行の「done HH:MM」。
    """
    lines = _norm(lines)
    return _read_codex(lines) if agent == "codex" else _read_claude(lines)


class StateTracker:
    """read_screen の結果をペインごとに覚えて、パネルの state（run / ask / done / idle）と since を決める。

    - run / ask はそのまま。ready は「直前が run / ask」か「新しいターン終了行が出た」なら done、それ以外は前の状態のまま
      （初めて見たペインに終了行があれば done。spike を再起動しても your turn を落とさないため）
    - done はユーザーがそのペインを見たら（iTerm2 が前面でそのペインがフォーカス中）idle
    - since は状態が変わった時刻。初めて見た時はスピナーの経過時間か「done HH:MM」から逆算し、読めなければ None
    - prompt / summary は画面から消えても最後に読めた値を残す
    """

    def __init__(self) -> None:
        self.mem: dict[str, dict] = {}

    def update(self, sid: str, raw: dict, focused: bool, now: float) -> dict:
        m = self.mem.get(sid)
        prev = m["state"] if m else None
        state = raw["state"]
        if state == "ready":
            fresh = raw.get("marker") and (m is None or raw["marker"] != m.get("marker"))
            state = "done" if prev in ("run", "ask") or fresh else (prev or "idle")
            if state == "done" and focused:
                state = "idle"
        if state == prev:
            since = m["since"]
        elif m is not None:
            since = now
        elif state == "run" and raw.get("elapsed") is not None:
            since = now - raw["elapsed"]
        else:
            since = clock_to_epoch(raw.get("done_at"), now)
        prompt = raw.get("prompt") or (m or {}).get("prompt")
        summary = raw.get("summary") or (m or {}).get("summary") if state in ("done", "idle") else None
        marker = raw.get("marker") or (m or {}).get("marker")
        self.mem[sid] = {"state": state, "since": since, "prompt": prompt, "summary": summary, "marker": marker}
        message = raw.get("message") if state in ("run", "ask") else summary
        return {"state": state, "since": since, "tool": raw.get("tool") if state in ("run", "ask") else None,
                "arg": raw.get("arg") if state in ("run", "ask") else None, "prompt": prompt, "message": message}

    def last(self, sid: str) -> dict | None:
        """画面が読めなかった時に出す、最後に分かっていた状態。"""
        m = self.mem.get(sid)
        if m is None:
            return None
        return {"state": m["state"], "since": m["since"], "tool": None, "arg": None, "prompt": m["prompt"],
                "message": m["summary"] if m["state"] in ("done", "idle") else None}

    def prune(self, alive: set[str]) -> None:
        for sid in set(self.mem) - alive:
            del self.mem[sid]


TRACKER = StateTracker()


# ---------- iTerm2 ----------

SCREEN_TAIL = 50   # 状態の判定に読む画面下部の行数。全画面（約 90 行）より 6 倍ほど軽い
PANE_TIMEOUT = 3.0  # 1 ペインの読み取りがこれを超えたら、そのペインだけ諦める


async def read_tail(s: iterm2.Session, n: int = SCREEN_TAIL) -> list[str]:
    """画面（スクロールバックを除く可変領域）の下から n 行。"""
    li = await s.async_get_line_info()
    end = li.overflow + li.scrollback_buffer_height + li.mutable_area_height
    first = max(li.overflow, end - n)
    return [x.string for x in await s.async_get_contents(first, end - first)]


async def _probe(s: iterm2.Session) -> dict | None:
    """1 ペイン分。agent でなければ None。画面の読み取り失敗は screen_error に入れて、ペイン自体は出す。"""
    job = await s.async_get_variable("jobName") or ""
    agent = agent_of(job)
    if agent == "none":
        return None
    cwd = await s.async_get_variable("path") or ""
    try:
        return {"agent": agent, "cwd": cwd, "screen": read_screen(await read_tail(s), agent), "screen_error": None}
    except Exception as exc:  # noqa: BLE001  画面が読めなくても一覧には出す
        return {"agent": agent, "cwd": cwd, "screen": None, "screen_error": exc}


async def snapshot(app: iterm2.App) -> dict:
    win = app.current_terminal_window
    tab = win.current_tab if win else None
    cur = tab.current_session.session_id if tab and tab.current_session else None
    # app_active は iTerm2 が前面か。分からない（None）時は前面とみなす
    looking = getattr(app, "app_active", None) is not False
    panes = [(tab, s) for window in app.terminal_windows for tab in window.tabs for s in tab.sessions]
    # 全ペインを並列に読む（直列だと 7 ペインの画面取得だけで 75ms ほどかかる）
    probes = await asyncio.gather(*(asyncio.wait_for(_probe(s), PANE_TIMEOUT) for _, s in panes),
                                  return_exceptions=True)
    sessions, errors = [], []
    now = time.time()
    for (tab, s), p in zip(panes, probes):
        if isinstance(p, BaseException):  # 仕様変更で 1 ペインだけ失敗しても一覧は出す
            errors.append(f"pane {s.session_id[:8]}: {p!r}")
            continue
        if p is None:
            continue
        if p["screen"] is not None:
            st = TRACKER.update(s.session_id, p["screen"], looking and s.session_id == cur, now)
        else:
            errors.append(f"pane {s.session_id[:8]} screen: {p['screen_error']!r}")
            st = TRACKER.last(s.session_id) or {"state": "idle", "since": None, "tool": None, "arg": None,
                                                 "prompt": None, "message": None}
        cwd = p["cwd"]
        sessions.append({
            "id": s.session_id, "tab": tab.tab_id,
            "name": os.path.basename(cwd.rstrip("/")) or cwd, "cwd": cwd, "agent": p["agent"],
            "branch": None, "dirty": False, **st,
        })
    TRACKER.prune({s.session_id for (_, s), p in zip(panes, probes) if p is not None})  # agent をやめたペインは忘れる
    _disambiguate(sessions)
    for x in sessions:
        x["group"], x["group_src"] = resolve_group(x["cwd"], STORE.groups, STORE.session_groups)
    fault ={"title": "some panes could not be read", "detail": errors[0], "hint": "spike log"} if errors else None
    return {
        "active": cur if any(x["id"] == cur for x in sessions) else None,
        "fault": fault, "sessions": sessions,
        "groups": STORE.groups, "profiles": STORE.profiles, "history": [],
    }


def _session(app: iterm2.App, sid) -> iterm2.Session:
    s = app.get_session_by_id(str(sid)) if sid else None
    if s is None:
        raise LookupError(f"session {sid} is gone")
    return s


def _target_tab(app: iterm2.App, sid) -> iterm2.Tab:
    if sid:
        _, tab = app.get_window_and_tab_for_session(_session(app, sid))
    else:
        win = app.current_terminal_window
        tab = win.current_tab if win else None
    if tab is None or not tab.sessions:
        raise LookupError("no tab to capture")
    return tab


async def capture(app: iterm2.App, sid) -> tuple[dict, str]:
    """タブの 1 つ目のペインを左、2 つ目を右として取り込む。3 つ目以降は無視してメッセージで知らせる。"""
    panes = _target_tab(app, sid).sessions

    async def one(s: iterm2.Session) -> dict:
        job = await s.async_get_variable("jobName") or ""
        return {"cwd": await s.async_get_variable("path") or "", "cmd": cmd_for(job)}

    left = await one(panes[0])
    right = await one(panes[1]) if len(panes) > 1 else None
    ratio = ratio_of(panes[0].grid_size.width, panes[1].grid_size.width) if right else 50
    name = os.path.basename(left["cwd"].rstrip("/")) or left["cwd"]
    note = f"{len(panes)} panes — captured the first two" if len(panes) > 2 else ""
    return {"name": name, "left": left, "right": right, "ratio": ratio}, note


async def open_profile(app: iterm2.App, p: dict) -> None:
    """profile から新しいタブを開く。既に同じ profile のタブがあっても、押すたびに 1 つ作る。

    分割比（ratio）は spike では未対応。async_split_pane は 50:50 で分ける。
    """
    win = app.current_terminal_window
    if win is None:
        win = await iterm2.Window.async_create(HUB.conn)
        if win is None:
            raise RuntimeError("could not create a window")
        tab = win.current_tab
    else:
        tab = await win.async_create_tab()
    if tab is None or tab.current_session is None:
        raise RuntimeError("could not create a tab")
    left = tab.current_session
    await left.async_send_text(shell_line(p["left"]))
    if p.get("right"):
        right = await left.async_split_pane(vertical=True)
        await right.async_send_text(shell_line(p["right"]))
        await left.async_activate(select_tab=True, order_window_front=True)
    try:
        await tab.async_set_title(p["name"])
    except Exception as exc:  # タイトルは付かなくてもタブは使える
        print(f"[warn] set title: {exc!r}", flush=True)
    p["last_used"] = time.time()


async def apply(op: str, a: dict) -> dict:
    """パネルからの操作。すべて asyncio 側で動かす。終わったらすぐ新しい状態を配る。"""
    app = await iterm2.async_get_app(HUB.conn)
    res: dict = {"msg": ""}
    dirty = True
    if op == "focus":
        s = _session(app, a.get("id"))
        await s.async_activate(select_tab=True, order_window_front=True)
        res["msg"] = f"focused {os.path.basename((await s.async_get_variable('path') or '').rstrip('/'))}"
        dirty = False
    elif op == "capture":
        res["profile"], res["msg"] = await capture(app, a.get("from"))
        dirty = False
    elif op == "open":
        p = STORE.profile(a.get("id"))
        await open_profile(app, p)
        res["msg"] = f"opened {p['name']}"
    elif op == "save":
        body = normalize_profile(a)
        gid = STORE.group_or_none(a.get("group"))
        if a.get("id"):
            p = STORE.profile(a["id"])
            STORE.check_profile_name(body["name"], p)
            p.update(body, group=gid)
            res["msg"] = f"updated {p['name']}"
        else:
            STORE.check_profile_name(body["name"])
            STORE.profiles.append({"id": "p-" + secrets.token_hex(4), **body, "group": gid, "last_used": None})
            res["msg"] = f"saved {body['name']}"
    elif op == "profile.delete":
        p = STORE.profile(a.get("id"))
        STORE.profiles.remove(p)
        res["msg"] = f"deleted {p['name']}"
    elif op == "profile.move":
        p = STORE.profile(a.get("id"))
        p["group"] = STORE.group_or_none(a.get("group"))
        STORE.profiles.remove(p)
        STORE.profiles.append(p)  # 移動先のグループでは末尾に置く
        res["msg"] = f"moved {p['name']} → {STORE.group(p['group'])['name'] if p['group'] else 'ungrouped'}"
    elif op == "profile.update":  # 同じグループの中で 1 つ上 / 下と入れ替える
        p = STORE.profile(a.get("id"))
        idx = [i for i, x in enumerate(STORE.profiles) if x.get("group") == p.get("group")]
        k = idx.index(STORE.profiles.index(p)) + int(a.get("dir") or 0)
        if 0 <= k < len(idx):
            i, j = STORE.profiles.index(p), idx[k]
            STORE.profiles[i], STORE.profiles[j] = STORE.profiles[j], STORE.profiles[i]
    elif op == "session.move":  # 所属は cwd 単位で記憶する。group: gid / None（明示 ungrouped）/ "auto"
        cwd = await _session(app, a.get("id")).async_get_variable("path") or ""
        res["msg"] = STORE.move_session(cwd, a.get("group"))
    elif op == "group.create":
        name = str(a.get("name") or "").strip()
        if not name:
            raise ValueError("type a name")
        STORE.check_group_name(name)
        dirs = normalize_dirs(a.get("dirs"))
        STORE.check_dirs(dirs)
        # 移す対象を先に確かめてから作る（途中で失敗してグループだけ残らないように）
        p = STORE.profile(a["move"]) if a.get("move") else None
        cwd = (await _session(app, a["move_session"]).async_get_variable("path") or "") if a.get("move_session") else None
        if cwd is not None and not cwd_key(cwd):
            raise ValueError("this session has no dir")
        color =a.get("color") if a.get("color") in GCOLORS else GCOLORS[0]
        g = {"id": "g-" + secrets.token_hex(4), "name": name, "color": color, **GROUP_DEFAULTS, "dirs": dirs}
        STORE.groups.append(g)
        res["msg"] = f"created group {name}"
        if p:
            p["group"] = g["id"]
            res["msg"] += f" · moved {p['name']}"
        if cwd is not None:
            STORE.move_session(cwd, g["id"])
            res["msg"] += f" · moved {os.path.basename(cwd_key(cwd))}"
    elif op == "group.update":
        g = STORE.group(a.get("gid"))
        if a.get("name") is not None:
            name = str(a["name"]).strip()
            if not name:
                raise ValueError("type a name")
            STORE.check_group_name(name, g)
            g["name"], res["msg"] = name, f"updated group {name}"
        if a.get("color") is not None:
            if a["color"] not in GCOLORS:
                raise ValueError(f"unknown color {a['color']}")
            g["color"] = a["color"]
        if a.get("dirs") is not None:
            dirs = normalize_dirs(a["dirs"])
            STORE.check_dirs(dirs, g)
            g["dirs"] = dirs
            res["msg"] = res["msg"] or f"group {g['name']}: {len(dirs)} dir rule{'s' if len(dirs) != 1 else ''}"
        if a.get("collapsed") is not None:
            g["collapsed"] = bool(a["collapsed"])
        if a.get("collapsed_sessions") is not None:
            g["collapsed_sessions"] = bool(a["collapsed_sessions"])
        if a.get("dir"):
            i = STORE.groups.index(g)
            j = i + int(a["dir"])
            if 0 <= j < len(STORE.groups):
                STORE.groups[i], STORE.groups[j] = STORE.groups[j], STORE.groups[i]
    elif op == "group.delete":
        g = STORE.group(a.get("gid"))
        STORE.groups.remove(g)
        for p in STORE.profiles:
            if p.get("group") == g["id"]:
                p["group"] = None
        # このグループを指す手動の記憶も消す（残すと同じ cwd のセッションが「明示 ungrouped」でもないのに浮く）
        STORE.session_groups = {k: v for k, v in STORE.session_groups.items() if v != g["id"]}
        res["msg"] = f"deleted group {g['name']} (its profiles and sessions → ungrouped)"
    elif op == "group.open":
        g = STORE.group(a.get("gid"))
        ps = [p for p in STORE.profiles if p.get("group") == g["id"]]
        for p in ps:
            await open_profile(app, p)
        res["msg"] = f"opened {len(ps)} tabs from {g['name']}"
    else:
        raise NotImplementedError(f"'{op}' is not supported in the spike")
    if dirty:
        STORE.save()
    HUB.publish(json.dumps(await snapshot(app), ensure_ascii=False))
    return res


# ---------- HTTP ----------

class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # アクセスログは出さない
        pass

    def _send(self, code: int, body: str, ctype: str = "application/json") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authed(self, q: dict) -> bool:
        got = self.headers.get("X-Saba-Token") or q.get("t", [""])[0]
        return secrets.compare_digest(got, TOKEN)

    def do_GET(self) -> None:
        u = urllib.parse.urlsplit(self.path)
        if not u.path.startswith("/api/"):
            return super().do_GET()
        q = urllib.parse.parse_qs(u.query)
        if not self._authed(q):
            return self._send(403, '{"error":"bad token"}')
        if u.path == "/api/state":
            return self._send(200, HUB.snap)
        if u.path == "/api/complete":
            # profiles は asyncio 側の持ち物だが、読むだけなのでスナップショット経由で拾う
            known = {os.path.expanduser(p["left"]["cwd"]).rstrip("/") for p in json.loads(HUB.snap).get("profiles", [])}
            return self._send(200, json.dumps(complete(q.get("p", [""])[0], known), ensure_ascii=False))
        if u.path == "/api/events":
            return self._events()
        return self._send(404, '{"error":"not found"}')

    def _events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        ver = -1
        try:
            while True:
                nv, snap = HUB.wait(ver, 15)
                self.wfile.write(b": ping\n\n" if nv == ver else f"event: state\ndata: {snap}\n\n".encode())
                self.wfile.flush()
                ver = nv
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:
        u = urllib.parse.urlsplit(self.path)
        if u.path != "/api/action" or not self._authed({}):
            return self._send(403, '{"error":"forbidden"}')
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._send(400, '{"error":"bad json"}')
        op = str(body.get("op"))
        try:
            res = HUB.call(apply(op, body))
        except (ValueError, LookupError, NotImplementedError) as exc:  # 入力や状態の問題。パネルにそのまま出す
            return self._send(400, json.dumps({"error": str(exc)}))
        except Exception as exc:  # iTerm2 側の失敗。止まらずにパネルへエラーを返す
            print(f"[error] {op} {body}: {exc!r}", flush=True)
            return self._send(500, json.dumps({"error": f"{op} failed: {exc!r}"}))
        return self._send(200, json.dumps(res, ensure_ascii=False))


def _start_http() -> int:
    handler = functools.partial(Handler, directory=str(DESIGN_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


async def _poll(app: iterm2.App) -> None:
    last: dict | None = None
    while True:
        try:
            last = await snapshot(app)
        except Exception as exc:  # iTerm2 側が壊れても止めずにエラーだけ出す
            base = last or {"active": None, "sessions": [], "groups": STORE.groups,
                            "profiles": STORE.profiles, "history": []}
            last = {**base, "fault": {"title": "iTerm2 API call failed", "detail": repr(exc), "hint": "spike log"}}
        HUB.publish(json.dumps(last, ensure_ascii=False))
        await asyncio.sleep(1)


async def main(connection: iterm2.Connection, mock: bool) -> None:
    HUB.loop, HUB.conn = asyncio.get_running_loop(), connection
    app = await iterm2.async_get_app(connection)
    HUB.publish(json.dumps(await snapshot(app), ensure_ascii=False))
    port = _start_http()
    url = f"http://127.0.0.1:{port}/panel-mock.html?" + ("mock" if mock else f"t={TOKEN}")
    # 別プロセス（hook スクリプトなど）が接続先を知るためのファイル。本人だけ読める権限で置く
    info = STORE_PATH.parent / "spike-panel.json"
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text(json.dumps({"url": url, "port": port, "token": TOKEN, "pid": os.getpid()}))
    info.chmod(0o600)
    try:
        await iterm2.tool.async_register_web_view_tool(connection, "saba-cli", TOOL_ID, True, url)
        print(f"[ok] toolbelt tool registered ({'mock' if mock else 'live'}): {url}")
    except Exception as exc:
        print(f"[error] toolbelt registration failed: {exc!r}")
    snap = json.loads(HUB.snap)
    print(f"sessions: {len(snap['sessions'])}  profiles: {len(snap['profiles'])}  — Ctrl-C to stop", flush=True)
    await _poll(app)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="モックデータで表示する")
    args = ap.parse_args()
    iterm2.run_forever(lambda conn: main(conn, args.mock))
