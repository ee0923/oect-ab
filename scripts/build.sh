#!/usr/bin/env bash
set -euo pipefail
kit=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd "$kit"
python3 scripts/verify-source.py source
[[ $(uname -m) == x86_64 ]]
[[ $(. /etc/os-release; echo "$VERSION_CODENAME") == noble ]]
[[ ! -e dist ]]
mkdir dist
# Use dependencies from the pinned checkout, not a mutable remote install script.
read -r -a packages < <(cat source/compile-kernel/tools/script/ubuntu2404-build-armbian-depends; echo)
sudo apt-get update
sudo apt-get install -y --no-install-recommends "${packages[@]}"
available=$(df -Pk . | awk 'NR==2 {print $4}')
(( available > 20 * 1024 * 1024 )) || { echo 'Need more than 20 GiB free space' >&2; exit 1; }
(
  cd source
  sudo ./rebuild -b wxy-oect -t ext4 -s 512/3000 -n oect-patch
) 2>&1 | tee dist/build.log
python3 scripts/verify-source.py source
shopt -s nullglob
images=(source/build/output/images/Armbian_*_rockchip_wxy-oect_bookworm_6.12.*_server_*.img.gz)
[[ ${#images[@]} == 1 ]] || { echo 'Expected exactly one OECT bookworm 6.12 server image' >&2; exit 1; }
bash scripts/verify-image.sh "${images[0]}"
mv "${images[0]}" dist/
cp sources.lock.json build.json base-input.json customize.sha256 dist/
(
  cd source
  find build-armbian/u-boot/rockchip/wxy-oect build-armbian/kernel/rk35xx \
    -type f -print0 | sort -z | xargs -0 sha256sum
) > dist/hardware-inputs.sha256
python3 - <<'PY'
import json, os
from pathlib import Path
Path('dist/build-record.json').write_text(json.dumps({
    'repository': os.environ.get('GITHUB_REPOSITORY'),
    'commit': os.environ.get('GITHUB_SHA'),
    'run_id': os.environ.get('GITHUB_RUN_ID'),
    'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
    'verification': 'GPT, boot offset, bootloader, env, extlinux, DTB, kernel, modules and Debian 12 rootfs checked; no device boot test',
    'mutable_inputs': ['Debian/Ubuntu apt packages', 'ophub firmware and u-boot main', 'latest rk35xx 6.12 kernel', 'base image unless explicitly configured'],
}, indent=2) + '\n')
PY
(cd dist && sha256sum -- *.img.gz *.json *.sha256 build.log > SHA256SUMS)
