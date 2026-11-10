#!/usr/bin/env bash
# Read-only inspection of the finished disk image. Never targets a physical disk.
set -euo pipefail
kit=$(cd -- "$(dirname -- "$0")/.." && pwd)
image=$(realpath -- "${1:?Pass an OECT .img.gz file}")
[[ -f "$image" && "$image" == *.img.gz ]]
work=$(mktemp -d)
loop=''
cleanup() {
  set +e
  mountpoint -q "$work/root" && sudo umount "$work/root"
  mountpoint -q "$work/boot" && sudo umount "$work/boot"
  [[ -z "$loop" ]] || sudo losetup -d "$loop"
  rm -rf -- "$work"
}
trap cleanup EXIT
gzip -dc -- "$image" > "$work/disk.img"
loop=$(sudo losetup --find --show --partscan --read-only "$work/disk.img")
sudo sfdisk --json "$loop" > "$work/partitions.json"
python3 - "$work/partitions.json" <<'PY'
import json, sys
t = json.load(open(sys.argv[1]))['partitiontable']
assert t['label'] == 'gpt' and len(t['partitions']) == 2, t
assert t['partitions'][0]['start'] * t['sectorsize'] == 180 * 1024 * 1024, t
PY
bootloader="$kit/source/build-armbian/u-boot/rockchip/wxy-oect"
python3 - "$work/disk.img" "$bootloader" <<'PY'
from pathlib import Path
import sys
with open(sys.argv[1], 'rb') as image:
    for name, source_offset, target_offset in [('bootloader.bin', 32768, 32768), ('env.bin', 0, 150994944)]:
        data = (Path(sys.argv[2]) / name).read_bytes()[source_offset:]
        assert data, name
        image.seek(target_offset)
        assert image.read(len(data)) == data, name
PY
mkdir "$work/boot" "$work/root"
sudo mount -t ext4 -o ro,noload "${loop}p1" "$work/boot"
sudo mount -t ext4 -o ro,noload "${loop}p2" "$work/root"
sudo python3 - "$work" <<'PY'
from pathlib import Path
import re, subprocess, sys
work = Path(sys.argv[1]); boot = work / 'boot'; root = work / 'root'
release = (root / 'etc/os-release').read_text()
assert re.search(r'^ID=["\x27]?debian["\x27]?$', release, re.M), release
assert re.search(r'^VERSION_CODENAME=["\x27]?trixie["\x27]?$', release, re.M), release
assert re.search(r'^VERSION_ID=["\x27]?13["\x27]?$', release, re.M), release
config = (boot / 'extlinux/extlinux.conf').read_text()
assert 'rk3566-wxy-oec-turbo-4g.dtb' in config
assert 'rootfstype=ext4' in config
uuid = subprocess.check_output(['findmnt', '-no', 'UUID', '--target', str(root)], text=True).strip()
assert uuid and f'root=UUID={uuid}' in config, config
for name in ('Image', 'uInitrd', 'dtb/rockchip/rk3566-wxy-oec-turbo-4g.dtb'):
    path = boot / name
    assert path.resolve().is_relative_to(boot.resolve()), name
    assert path.is_file() and path.stat().st_size > 0, name
assert (boot / 'dtb/rockchip/rk3566-wxy-oec-turbo-4g.dtb').read_bytes()[:4] == b'\xd0\x0d\xfe\xed'
kernel = (boot / 'Image').resolve().name.removeprefix('vmlinuz-')
assert re.fullmatch(r'6\.12\.\d+[-A-Za-z0-9._+]*', kernel), kernel
assert (root / 'usr/lib/modules' / kernel / 'kernel').is_dir(), kernel
assert "BOARD='wxy-oect'" in (root / 'etc/ophub-release').read_text()
print('Read-only OECT image verification passed')
PY
