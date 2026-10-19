#!/usr/bin/env bash
set -euo pipefail
[[ ${EUID} -eq 0 ]] || { echo 'Run on the existing VPS as root'; exit 1; }
[[ $# -eq 1 && $1 =~ ^[0-9a-f]{40}$ ]] || { echo 'Pass the pinned release commit'; exit 1; }
test -f /etc/systemd/system/medpravo-relay.service
task_update_dir=$(mktemp -d /tmp/medpravo-update.XXXXXX)
trap 'rm -f -- "$task_update_dir/russian_proxy.py" "$task_update_dir/network.py" "$task_update_dir/sources.json"; rmdir -- "$task_update_dir"' EXIT
for task_file in russian_proxy.py network.py sources.json; do
  curl --fail --location --retry 2 --silent --show-error "https://raw.githubusercontent.com/lov5ik/medpravo/$1/$task_file" -o "$task_update_dir/$task_file"
done
python3 - "$task_update_dir" <<'PY'
import ast,json,sys
from pathlib import Path
root=Path(sys.argv[1])
for name in ('russian_proxy.py','network.py'):ast.parse((root/name).read_text())
sources=json.loads((root/'sources.json').read_text())
assert len(sources)==26 and all(s['allowed_domains'] for s in sources)
PY
install -m 644 "$task_update_dir/russian_proxy.py" "$task_update_dir/network.py" "$task_update_dir/sources.json" /opt/medpravo-relay/
systemctl restart medpravo-relay
systemctl is-active --quiet medpravo-relay
python3 - <<'PY'
import json,time,urllib.request
for attempt in range(10):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8118/health',timeout=2) as response:data=json.load(response)
        assert data.get('http_sources') is True
        print('MedPravo relay updated: HTTP + HTTPS, loopback only');break
    except OSError:
        if attempt==9:raise
        time.sleep(1)
PY
