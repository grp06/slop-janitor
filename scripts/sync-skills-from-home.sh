#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_root="${HOME}/.agents/skills"
dest_root="${repo_root}/.agents/skills"

skills=(
  execplan-create
  execplan-improve
  find-best-refactor
  implement-execplan
  review-recent-work
)

command -v rsync >/dev/null 2>&1 || {
  echo "rsync is required" >&2
  exit 1
}

for skill in "${skills[@]}"; do
  source_dir="${source_root}/${skill}"
  dest_dir="${dest_root}/${skill}"

  if [[ ! -d "${source_dir}" ]]; then
    echo "missing source skill: ${source_dir}" >&2
    exit 1
  fi

  if [[ -L "${dest_dir}" ]]; then
    unlink "${dest_dir}"
  fi

  mkdir -p "${dest_dir}"
  rsync -a --delete "${source_dir}/" "${dest_dir}/"
done
