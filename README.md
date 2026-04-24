# saba-cli

> A personal toolbelt of handy CLI utilities — starting with a pretty git tree.

`saba-cli` is a small, extensible command-line tool. The first feature is
`saba-cli --tree`: a clean, information-dense rendering of the git commit
graph that uses the [Iceberg](https://cocopon.github.io/iceberg.vim/) color
palette.

## Highlights

- **Zero runtime dependencies.** Pure Python 3.9+ stdlib. No pip install
  step required to try it — just run the launcher in `bin/`.
- **Iceberg-themed.** 24-bit ANSI colors that match the Iceberg palette
  exactly. Colors auto-disable when the output is not a TTY or when
  `NO_COLOR` / `--no-color` is set.
- **All the info you want in one line.** For each commit: graph node,
  short hash, refs (HEAD, local branch, remote branch, tags), relative
  date, author, and subject — column-aligned and trimmed to fit your
  terminal width.
- **Extensible.** New commands drop into `src/saba_cli/commands/` and
  register in `cli.py`.

## Install

### Quick try (no install)

```bash
./bin/saba-cli --tree
```

### As a proper command

```bash
pip install -e .
saba-cli --tree
```

### Put it on your `$PATH` without pip

```bash
ln -s "$PWD/bin/saba-cli" /usr/local/bin/saba-cli
```

## Usage

```bash
saba-cli --tree                 # pretty git tree of the current repo
saba-cli --tree --limit 20      # most recent 20 commits only
saba-cli --tree --no-all        # just the current branch
saba-cli --tree --no-color      # plain text, no ANSI
saba-cli --help
saba-cli --version
```

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
├── src/
│   └── saba_cli/
│       ├── __init__.py
│       ├── __main__.py       # enables `python -m saba_cli`
│       ├── cli.py            # argparse dispatcher
│       ├── theme.py          # Iceberg palette + ANSI helpers
│       ├── commands/
│       │   └── tree.py       # --tree implementation
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
