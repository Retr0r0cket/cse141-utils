#!/usr/bin/env bash
# Share the CSE 141 project tools between repos (see README.md).
#
#   setup.sh install [--force] [--no-hook] [REPO]   copy the shared files into REPO (pull)
#   setup.sh push    [--force] [--no-remote] [REPO] copy REPO's committed changes back here
#   setup.sh status  [REPO]                         what differs, file by file
#
# REPO is a project repo (default: the one you're in). files/ here mirrors a project's layout:
# files/tools/score.py is tools/score.py there. A project keeps its own committed copies, so
# nothing it builds or grades depends on this repo being around.
#
# install copies every shared file in. A file you've changed in REPO since it last synced is
# kept, unless --force. It links REPO/setup.sh to this script, and adds a post-commit hook to REPO, which runs `push` after every
# commit, so changes made there come back here (and to this repo's remote) by themselves.
# Pulling them into other repos is up to you: run install there when you want them.
#
# push takes REPO's files as of its last commit (not uncommitted edits): every shared file it
# changed, and new files where shared files live (tools/, test_programs/custom and stress/,
# testbench/*_tb.sv, docs/testing.md and tools.md, student.just). test_programs/submit/ is
# each week's own, so it's never shared. It commits them
# here, and pushes this repo unless --no-remote or `git config cse141-utils.autopush false`.
# A file changed both here and in REPO since they last synced is left alone, unless --force.
#
# What was last synced is kept per repo, in REPO/.git/cse141-utils/lock (sha256 and path).

set -euo pipefail

# Resolve symlinks: install links REPO/setup.sh back to this file
self=$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")
UTILS=$(cd "$(dirname "$self")" && pwd)
FILES=$UTILS/files

# The course's own testbenches: never shared, never touched
COURSE_FILES='testbench/program_tb.sv testbench/manager_tb.sv testbench/counter_apb_tb.sv'

usage() { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

cmd=${1:-}; [ -n "$cmd" ] || usage; shift
force=0 hook=1 remote=1 quiet=0 repo=
while [ $# -gt 0 ]; do
  case "$1" in
    --force)     force=1 ;;
    --no-hook)   hook=0 ;;
    --no-remote) remote=0 ;;
    --hook)      quiet=1 ;;   # run by the post-commit hook: say only what it did
    -*)          usage ;;
    *)           repo=$1 ;;
  esac
  shift
done

repo=$(git -C "${repo:-.}" rev-parse --show-toplevel 2>/dev/null) || { echo "not in a git repo: ${repo:-.}" >&2; exit 1; }
[ "$repo" != "$UTILS" ] || { echo "run this from a project repo (or name one), not from cse141-utils" >&2; exit 1; }
gitdir=$(cd "$repo" && cd "$(git rev-parse --git-dir)" && pwd)
lock=$gitdir/cse141-utils/lock
mkdir -p "$(dirname "$lock")"
touch "$lock"

say() { [ "$quiet" = 1 ] || echo "$@"; }
hash_file() { shasum -a 256 < "$1" | cut -d' ' -f1; }
hash_head() { git -C "$repo" show "HEAD:$1" | shasum -a 256 | cut -d' ' -f1; }
in_head()   { git -C "$repo" cat-file -e "HEAD:$1" 2>/dev/null; }
lock_get()  { awk -F'\t' -v p="$1" '$2 == p { print $1 }' "$lock"; }
lock_set() {
  awk -F'\t' -v p="$1" '$2 != p' "$lock" > "$lock.new"
  printf '%s\t%s\n' "$2" "$1" >> "$lock.new"
  sort -t "$(printf '\t')" -k2 "$lock.new" > "$lock"
  rm -f "$lock.new"
}

# Files this repo shares: everything under files/ (but iCloud's "name 2" copies and caches)
shared_here() {
  (cd "$FILES" && find . -type f ! -name '.DS_Store' ! -name '* [0-9]' ! -name '* [0-9].*' \
     ! -path '*/__pycache__/*' | sed 's|^\./||' | sort)
}

# Files in REPO's last commit that are shared, or would be if they were new
shareable_there() {
  git -C "$repo" ls-tree -r --name-only HEAD -- student.just tools test_programs/custom \
      test_programs/stress testbench docs/testing.md docs/tools.md 2>/dev/null |
    grep -v -e '/__pycache__/' -e '\.DS_Store$' |
    while read -r p; do
      case "$p" in
        testbench/*) case " $COURSE_FILES " in *" $p "*) continue ;; esac
                     case "$p" in *_tb.sv) ;; *) continue ;; esac ;;
      esac
      echo "$p"
    done
}

install_hook() {
  local h=$gitdir/hooks/post-commit marker='# cse141-utils'
  mkdir -p "$gitdir/hooks"
  git -C "$repo" config cse141-utils.path "$UTILS"
  if [ -f "$h" ] && grep -q "$marker" "$h"; then return; fi
  if [ ! -f "$h" ]; then printf '#!/bin/sh\n' > "$h"; fi
  cat >> "$h" <<'EOF'
# cse141-utils: share this commit's changes to the shared tools (see cse141-utils/README.md)
utils=$(git config cse141-utils.path) && [ -x "$utils/setup.sh" ] && "$utils/setup.sh" push --hook || true
EOF
  chmod +x "$h"
  say "post-commit hook added: commits here share their tool changes with $UTILS"
}

# REPO/setup.sh -> this script, so a project can run ./setup.sh push/status without the path
link_self() {
  local l=$repo/setup.sh
  if [ -L "$l" ]; then
    [ "$(readlink "$l")" = "$UTILS/setup.sh" ] || ln -sf "$UTILS/setup.sh" "$l"
  elif [ -e "$l" ]; then
    say "kept    setup.sh (a regular file is in the way of the symlink)"
  else
    ln -s "$UTILS/setup.sh" "$l"
    say "symlink added: $l -> $UTILS/setup.sh"
  fi
}

do_install() {
  local rel src dst up cur was n_new=0 n_upd=0 n_kept=0
  while read -r rel; do
    src=$FILES/$rel dst=$repo/$rel
    up=$(hash_file "$src")
    if [ ! -e "$dst" ]; then
      mkdir -p "$(dirname "$dst")"; cp -p "$src" "$dst"; lock_set "$rel" "$up"
      n_new=$((n_new + 1)); continue
    fi
    cur=$(hash_file "$dst")
    if [ "$cur" = "$up" ]; then lock_set "$rel" "$up"; continue; fi
    was=$(lock_get "$rel")
    if [ "$cur" = "$was" ] || [ "$force" = 1 ]; then
      cp -p "$src" "$dst"; lock_set "$rel" "$up"; n_upd=$((n_upd + 1))
    else
      say "kept    $rel (changed here: --force to overwrite, or commit it to push it)"
      n_kept=$((n_kept + 1))
    fi
  done < <(shared_here)
  say "installed into $repo: $n_new new, $n_upd updated, $n_kept kept as they were"
  link_self
  if [ "$hook" = 1 ]; then install_hook; fi
}

do_push() {
  local rel src cur up was mode conflicts=0
  local changed=()
  git -C "$repo" rev-parse -q --verify HEAD > /dev/null || { say "nothing committed yet"; return 0; }
  while read -r rel; do
    in_head "$rel" || continue
    src=$FILES/$rel
    cur=$(hash_head "$rel")
    up=; if [ -f "$src" ]; then up=$(hash_file "$src"); fi
    if [ "$cur" = "$up" ]; then lock_set "$rel" "$cur"; continue; fi
    was=$(lock_get "$rel")
    if [ -n "$was" ] && [ "$cur" = "$was" ]; then continue; fi   # not changed here: newer upstream
    if [ -n "$up" ] && [ "$up" != "$was" ] && [ "$force" = 0 ]; then
      echo "cse141-utils: $rel changed both here and in cse141-utils: not pushed (install, or push --force)" >&2
      conflicts=$((conflicts + 1)); continue
    fi
    mkdir -p "$(dirname "$src")"
    git -C "$repo" show "HEAD:$rel" > "$src"
    mode=$(git -C "$repo" ls-tree HEAD -- "$rel" | cut -d' ' -f1)
    if [ "$mode" = 100755 ]; then chmod +x "$src"; else chmod -x "$src"; fi
    lock_set "$rel" "$cur"
    changed+=("files/$rel")
  done < <({ shared_here; shareable_there; } | sort -u)
  if [ ${#changed[@]} -eq 0 ]; then say "nothing to push"; return "$((conflicts > 0))"; fi
  local name sha
  name=$(basename "$repo"); sha=$(git -C "$repo" rev-parse --short HEAD)
  git -C "$UTILS" add -- "${changed[@]}"
  git -C "$UTILS" commit -q -m "Sync ${#changed[@]} file(s) from $name $sha" \
      -m "$(printf '%s\n' "${changed[@]}" | sed 's|^files/||')" -- "${changed[@]}"
  echo "cse141-utils: ${#changed[@]} shared file(s) from $name committed to $UTILS"
  if [ "$remote" = 1 ] && [ "$(git -C "$repo" config cse141-utils.autopush || echo true)" != false ] &&
     [ -n "$(git -C "$UTILS" remote)" ]; then
    # In the background, so a slow or missing network never holds up a commit
    ( git -C "$UTILS" push -q > "$gitdir/cse141-utils/push.log" 2>&1 ||
      echo "cse141-utils: push failed (see $gitdir/cse141-utils/push.log)" >&2 ) &
  fi
  return "$((conflicts > 0))"
}

do_status() {
  local rel src dst cur up was st
  while read -r rel; do
    src=$FILES/$rel dst=$repo/$rel
    up=; if [ -f "$src" ]; then up=$(hash_file "$src"); fi
    cur=; if [ -f "$dst" ]; then cur=$(hash_file "$dst"); fi
    was=$(lock_get "$rel")
    if   [ -z "$up" ];        then st="only here (shared once committed)"
    elif [ -z "$cur" ];       then st="missing here (install adds it)"
    elif [ "$cur" = "$up" ];  then continue
    elif [ "$cur" = "$was" ]; then st="newer in cse141-utils (install updates it)"
    elif [ "$up" = "$was" ];  then st="changed here (shared once committed)"
    else                           st="changed in both"
    fi
    printf '%-56s %s\n' "$rel" "$st"
  done < <({ shared_here; shareable_there; } | sort -u)
}

case "$cmd" in
  install|pull) do_install ;;
  push)         do_push ;;
  status)       do_status ;;
  *)            usage ;;
esac
