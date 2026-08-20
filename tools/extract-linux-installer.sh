#!/bin/bash
SP=/private/tmp/claude-501/-Users-jayden-barcotest/c7766f5a-ca46-4b56-9ae5-d9218a975ee2/scratchpad
cd "$SP" || exit 1
BFM=/proc/sys/fs/binfmt_misc

setrosetta() {
  docker run --privileged --rm arm64v8/debian:12-slim bash -c "
    mount -t binfmt_misc none $BFM 2>/dev/null
    for f in $BFM/rosetta*; do [ -e \"\$f\" ] && echo $1 > \"\$f\"; done
    echo -n 'rosetta: '; head -1 $BFM/rosetta" 2>&1 | tail -2
}
restore() { echo "=== RESTORE rosetta ==="; setrosetta 1; }
trap restore EXIT

echo "=== install qemu-user-static binfmt handlers ==="
docker run --privileged --rm tonistiigi/binfmt --install amd64 2>&1 | tail -8

echo "=== disable rosetta ==="; setrosetta 0

echo "=== verify handler ==="
docker run --privileged --rm arm64v8/debian:12-slim bash -c "head -3 $BFM/qemu-x86_64" 2>&1 | tail -4

echo "=== install EventMaster via qemu ==="
rm -rf out6 && mkdir -p out6
docker run --rm --platform linux/amd64 -v "$SP/lin:/in:ro" -v "$SP/out6:/out" debian:12-slim bash -c '
  echo "arch=$(uname -m)"; export HOME=/root
  /in/EventMaster-9.2.68334-linux-x64-installer.run --mode unattended --prefix /out/EventMaster
  echo "RC=$?"
  du -sh /out 2>/dev/null; find /out -maxdepth 3 -type d 2>/dev/null | head -50
' 2>&1 | tail -60
