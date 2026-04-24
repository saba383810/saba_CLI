# saba-cli — fish shell tab completions
#
# Drop this file into ~/.config/fish/completions/ (install.fish does that
# automatically) and fish will pick it up on the next shell start.

# Disable default file/path completion — saba-cli takes no positional args yet.
complete -c saba-cli -f

# Commands (flag-style for now; will grow into subcommands)
complete -c saba-cli -l tree        -d "Render the git commit tree (Iceberg-themed)"

# Shared options
complete -c saba-cli -s n -l limit       -x -d "Limit number of commits shown (int, default 100)"
complete -c saba-cli       -l all            -d "Include all branches in the tree (default)"
complete -c saba-cli       -l no-all         -d "Restrict to the current branch only"
complete -c saba-cli       -l no-color       -d "Disable colored output (implies --static)"
complete -c saba-cli       -l static         -d "Print the static tree instead of the TUI"
complete -c saba-cli -s i -l interactive     -d "Force the interactive TUI even when not a TTY"

# Meta
complete -c saba-cli -s h -l help    -d "Show help"
complete -c saba-cli -s V -l version -d "Show version"
