# saba-cli

> A personal toolbelt of handy CLI utilities — starting with a pretty git tree.

`saba-cli` is a small, extensible command-line tool. The first feature is
`saba-cli --tree`: a clean, information-dense rendering of the git commit
graph that uses the [Iceberg](https://cocopon.github.io/iceberg.vim/) color
palette — with an **interactive TUI** when run in a terminal.

## Highlights

- **Interactive by default.** In a real TTY, `saba-cli --tree` opens an
  interactive curses viewer: arrow-key navigation, incremental search,
  `esc` to quit. Falls back to a static ANSI render when piped or
  redirected (or when `--static` is passed).
- **Zero runtime dependencies.** Pure Python 3.9+ stdlib. No pip install
  step required to try it — just run the launcher in `bin/`.
- **Iceberg-themed.** 24-bit colors matching the Iceberg palette
  exactly; auto-fallback to xterm-256 and then to 8-color on older
  terminals.
- **All the info you want in one line.** For each commit: graph node,
  short hash, refs (HEAD, local branch, remote branch, tags), relative
  date, author, and subject — column-aligned and trimmed to fit.
- **Extensible.** New commands drop into `src/saba_cli/commands/` and
  register in `cli.py`.

## Install

### One-shot install (recommended, fish shell)

```fish
./install.fish          # install — adds bin/ to $fish_user_paths + installs completions
./install.fish --check  # dry-run: show what would change
./install.fish --uninstall
```

What it does (idempotent, re-runnable):

1. Checks `python3` and `git` are available.
2. Makes `bin/saba-cli` executable and adds `bin/` to the **universal**
   `$fish_user_paths` so it persists across shell restarts and new tabs.
3. Installs tab completions to `~/.config/fish/completions/saba-cli.fish`.

After install, open a new fish tab (or run `exec fish`) and `saba-cli` is
on your `$PATH`, with `saba-cli --<Tab>` suggesting flags.

### Quick try (no install)

```bash
./bin/saba-cli --tree
```

### As a proper command via pip

```bash
pip install -e .
saba-cli --tree
```

This also wires up `saba-cli` as a console script via `pyproject.toml` —
handy if you want to install it into a virtualenv.

### Other shells

Put the repo's `bin/` on your `$PATH` manually:

```bash
# bash / zsh
echo 'export PATH="'"$PWD"'/bin:$PATH"' >> ~/.bashrc   # or ~/.zshrc
```

or symlink it:

```bash
ln -s "$PWD/bin/saba-cli" /usr/local/bin/saba-cli
```

## Usage

```bash
saba-cli --tree                 # interactive TUI (static when piped)
saba-cli --tree --mine          # start with the 'only my commits' filter
saba-cli --tree --no-details    # start with the detail pane hidden
saba-cli --tree --static        # skip the TUI, print once and exit
saba-cli --tree --limit 20      # most recent 20 commits only
saba-cli --tree --no-all        # just the current branch
saba-cli --tree --no-color      # plain text, implies --static
saba-cli --help
saba-cli --version
```

### Interactive layout

Two panes are visible when the terminal has room (auto-hidden when it
does not):

- **tree** (top) — the scrollable commit graph
- **detail** (bottom) — full hash, author + email, absolute & relative
  date, every ref, the full commit message, and `git show --stat`
  output with `+`/`-` colored

Commits by the current git user (`git config user.email`) get a `▎`
marker at the left edge and a bold amber author name, so it is easy to
spot your own work in a crowded tree. The filter `m` narrows the tree
to just those commits.

### Interactive key bindings (TUI)

| keys                    | action                                                    |
|-------------------------|-----------------------------------------------------------|
| `↑` / `k`               | move selection up                                         |
| `↓` / `j`               | move selection down                                       |
| `PgUp` / `PgDn`         | scroll one screen                                         |
| `Home` / `g`            | jump to newest                                            |
| `End`  / `G`            | jump to oldest                                            |
| `Tab` / `d`             | toggle the detail pane                                    |
| `[` / `]`               | scroll the detail pane up / down                          |
| `m`                     | toggle "only my commits" filter                           |
| `f` or `/`              | enter search mode (incremental filter)                    |
| `Enter`                 | in search: apply filter and resume navigation             |
| `Esc`                   | in search: cancel; with filter: clear; else quit          |
| `q` / `Q`               | quit                                                      |

Search matches across commit hash, author, refs, and subject.
Graph connector lines stay in place but the cursor skips over them.

### Adaptive layout for narrow terminals

When the pane you run `saba-cli` in is narrow, lower-priority columns
are dropped automatically so the subject always has room:

- width `≥ 90`: graph · mine · hash · refs · date · author · subject
- width `≥ 70`: graph · mine · hash · date · author · subject
- width `≥ 54`: graph · mine · hash · date · subject
- width `<  54`: graph · mine · hash · subject

The detail pane still shows the full information for the selected
commit regardless.

### What the tree shows

Each row contains:

| column  | meaning                                                   |
|---------|-----------------------------------------------------------|
| graph   | ASCII branch graph (from `git log --graph`), recolored    |
| hash    | abbreviated commit hash (`%h`)                            |
| refs    | `HEAD → branch`, local branches, `remote/branch`, `tag:`  |
| date    | relative author date (`2 hours ago`)                      |
| author  | commit author name                                        |
| subject | first line of the commit message, truncated to fit        |

Refs are parsed and colored by kind:

- **HEAD →** bright magenta, bold
- **local branch** green, bold
- **remote branch** red
- **tag** `◆ name`, amber, bold

## Project layout

```
saba_cli/
├── bin/
│   └── saba-cli              # dependency-free launcher
├── completions/
│   └── saba-cli.fish         # fish tab-completions
├── install.fish              # one-shot installer for the fish shell
├── src/
│   └── saba_cli/
│       ├── __init__.py
│       ├── __main__.py       # enables `python -m saba_cli`
│       ├── cli.py            # argparse dispatcher
│       ├── theme.py          # Iceberg palette + ANSI helpers
│       ├── commands/
│       │   ├── tree.py           # --tree dispatcher (static / TUI)
│       │   ├── _tree_common.py   # shared helpers (ref parse, graph glyphs, pad/trunc)
│       │   ├── _tree_static.py   # static ANSI renderer
│       │   └── _tree_tui.py      # interactive curses viewer
│       └── utils/
│           └── git.py        # git subprocess wrapper
├── pyproject.toml
├── LICENSE
└── README.md
```

## Adding a new command

1. Create `src/saba_cli/commands/<name>.py` with a `run(args) -> int`.
2. Register a flag (or subcommand) in `src/saba_cli/cli.py`.
3. Import lazily inside `_dispatch` so the help text keeps rendering even
   if a command module has issues.

## License

MIT — see [LICENSE](./LICENSE).
