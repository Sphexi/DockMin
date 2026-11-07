#!/usr/bin/env bash
# Mirror docs/, CHANGELOG.md and CONTRIBUTING.md into a wiki checkout and commit any change.
# Usage: publish_wiki.sh <source-root> <wiki-checkout> <source-revision>
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <source-root> <wiki-checkout> <source-revision>" >&2
  exit 2
fi

source_root=$1
wiki=$2
revision=$3

if [[ ! -d "$source_root/docs" ]]; then
  echo "no docs/ folder under $source_root" >&2
  exit 1
fi
if [[ ! -d "$wiki/.git" ]]; then
  echo "$wiki is not a git checkout" >&2
  exit 1
fi
# A symlink would publish its target's contents, which may lie outside the repository.
if [[ -n $(find "$source_root/docs" "$source_root/CHANGELOG.md" "$source_root/CONTRIBUTING.md" -type l -print -quit) ]]; then
  echo "symlinks are not published" >&2
  exit 1
fi
if [[ -n $(find "$source_root/docs" -mindepth 1 -name '.*' -print -quit) ]]; then
  echo "dotfiles in docs/ are not published" >&2
  exit 1
fi
if [[ -e "$source_root/docs/Changelog.md" || -e "$source_root/docs/Contributing.md" ]]; then
  echo "docs/Changelog.md and docs/Contributing.md are generated from the top-level files" >&2
  exit 1
fi

find "$wiki" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf -- {} +
cp -R -- "$source_root/docs/." "$wiki/"
cp -- "$source_root/CHANGELOG.md" "$wiki/Changelog.md"
cp -- "$source_root/CONTRIBUTING.md" "$wiki/Contributing.md"

git -C "$wiki" add --all
if git -C "$wiki" diff --cached --quiet; then
  echo "The wiki already matches $revision."
  exit 0
fi
git -C "$wiki" \
  -c user.name="github-actions[bot]" \
  -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
  commit --quiet -m "Publish documentation from $revision"
git -C "$wiki" --no-pager log -1 --stat --format="%h %s"
