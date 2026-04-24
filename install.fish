#!/usr/bin/env fish
#
# install.fish — one-shot saba-cli installer for the fish shell.
#
# What it does (idempotent):
#   1. Verifies python3 and git are on $PATH.
#   2. Makes bin/saba-cli executable and adds bin/ to $fish_user_paths
#      (universal scope, so it survives shell restarts).
#   3. Installs tab-completions to ~/.config/fish/completions/saba-cli.fish.
#
# Usage:
#   ./install.fish            # install
#   ./install.fish --uninstall
#   ./install.fish --check    # dry-run: show what would happen

# NOTE: `set -g` so the paths are visible inside the functions below.
# Fish's `set -l` at script scope would hide them from nested functions.
set -g here (status filename)
set -g repo_root (cd (dirname $here); and pwd -P)
set -g bin_dir $repo_root/bin
set -g comp_src $repo_root/completions/saba-cli.fish
set -g comp_dst $__fish_config_dir/completions/saba-cli.fish

# ---- Iceberg-ish color helpers -------------------------------------------------
function _ice_title;   set_color -o 91acd1; end    # blue bright, bold
function _ice_ok;      set_color       c0ca8e; end # green
function _ice_warn;    set_color       e9b189; end # yellow
function _ice_err;     set_color -o    e98989; end # red bold
function _ice_muted;   set_color       6b7089; end # dim gray-blue
function _ice_reset;   set_color       normal; end

function _say_step
    echo (_ice_muted)"» "(_ice_reset)$argv
end
function _say_ok
    echo (_ice_ok)"✓ "(_ice_reset)$argv
end
function _say_warn
    echo (_ice_warn)"! "(_ice_reset)$argv
end
function _say_err
    echo (_ice_err)"✗ "(_ice_reset)$argv
end

# ---- Actions -------------------------------------------------------------------
function _check_deps
    set -l missing
    for cmd in python3 git
        if not command -q $cmd
            set missing $missing $cmd
        end
    end
    if test (count $missing) -gt 0
        _say_err "missing dependencies: $missing"
        echo "   install them and re-run ./install.fish"
        return 1
    end
    _say_ok "deps found — python3 "(python3 --version 2>&1 | string trim)", git "(git --version | string trim)
    return 0
end

function _do_install
    _say_step "installing saba-cli from "(_ice_muted)$repo_root(_ice_reset)
    _check_deps; or return 1

    # 1. Make the launcher executable
    chmod +x $bin_dir/saba-cli
    _say_ok "bin/saba-cli is executable"

    # 2. Persist bin/ on PATH.
    # `fish_add_path` with no scope flag targets the universal
    # $fish_user_paths, which survives shell restarts and is shared
    # across all fish sessions — exactly what we want.
    if contains -- $bin_dir $fish_user_paths
        _say_ok "already on \$fish_user_paths: "(_ice_muted)$bin_dir(_ice_reset)
    else
        fish_add_path -U $bin_dir
        _say_ok "added to universal \$fish_user_paths: "(_ice_muted)$bin_dir(_ice_reset)
    end

    # 3. Completions
    mkdir -p (dirname $comp_dst)
    cp $comp_src $comp_dst
    _say_ok "installed completions: "(_ice_muted)$comp_dst(_ice_reset)

    echo
    echo (_ice_title)"saba-cli is ready."(_ice_reset)
    echo "  try: "(_ice_ok)"saba-cli --tree"(_ice_reset)
    echo "  new shells pick this up automatically; for this shell run:"
    echo "       "(_ice_muted)"exec fish"(_ice_reset)"     # or just open a new tab"
end

function _do_uninstall
    _say_step "uninstalling saba-cli"

    if contains -- $bin_dir $fish_user_paths
        set -l idx (contains -i -- $bin_dir $fish_user_paths)
        set --erase fish_user_paths[$idx]
        _say_ok "removed from \$fish_user_paths: "(_ice_muted)$bin_dir(_ice_reset)
    else
        _say_warn "$bin_dir was not in \$fish_user_paths — nothing to remove"
    end

    if test -f $comp_dst
        rm -f $comp_dst
        _say_ok "removed completions: "(_ice_muted)$comp_dst(_ice_reset)
    else
        _say_warn "no completions file at $comp_dst"
    end

    echo
    echo (_ice_title)"saba-cli uninstalled (repo files untouched)."(_ice_reset)
end

function _do_check
    _say_step "dry-run — nothing will change"
    _check_deps
    echo
    echo "would add to \$fish_user_paths: "(_ice_muted)$bin_dir(_ice_reset)
    if contains -- $bin_dir $fish_user_paths
        _say_ok "(already present)"
    else
        _say_warn "(not present)"
    end
    echo "would install completions to: "(_ice_muted)$comp_dst(_ice_reset)
    if test -f $comp_dst
        _say_ok "(already installed)"
    else
        _say_warn "(not installed)"
    end
end

# ---- Main ----------------------------------------------------------------------
switch "$argv[1]"
    case --uninstall -u
        _do_uninstall
    case --check -n --dry-run
        _do_check
    case '' --install -i
        _do_install
    case -h --help
        echo "usage: ./install.fish [--install | --uninstall | --check]"
        echo ""
        echo "  --install     (default) add bin/ to PATH and install completions"
        echo "  --uninstall   undo the above"
        echo "  --check       dry-run — print what would change"
    case '*'
        _say_err "unknown option: $argv[1]"
        echo "try: ./install.fish --help"
        exit 2
end
