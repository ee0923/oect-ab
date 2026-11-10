#!/usr/bin/env bash
set -euo pipefail
kit=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd "$kit"
source_dir=${1:-source}
[[ ! -e "$source_dir" && ! -L "$source_dir" ]] || { echo 'Source destination already exists' >&2; exit 1; }
commit=$(jq -er '.upstream.commit' sources.lock.json)
repository=$(jq -er '.upstream.repository' sources.lock.json)
[[ "$commit" =~ ^[0-9a-f]{40}$ ]]
[[ "$repository" == https://github.com/ophub/amlogic-s9xxx-armbian.git ]]
git init "$source_dir"
git -C "$source_dir" remote add origin "$repository"
git -C "$source_dir" -c protocol.file.allow=never fetch --depth 1 origin "$commit"
git -C "$source_dir" checkout --detach FETCH_HEAD
[[ $(git -C "$source_dir" rev-parse HEAD) == "$commit" ]]
git -C "$source_dir" -c protocol.file.allow=never submodule update --init --recursive --depth 1
