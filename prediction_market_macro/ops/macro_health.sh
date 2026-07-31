#!/bin/bash
# ops/macro_health.sh — one-shot operator health view (§15 mother-template port).
# Prints the latest macro_health.json summary + loaded launchd jobs + last refresh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HP="$ROOT/data/output/macro_health.json"
echo "── launchd ──"
launchctl list | grep -E "someopark\.macro" || echo "none loaded"
echo "── last refresh ──"
[ -f "$ROOT/data/output/refresh_last.json" ] \
  && python3 -c "import json;d=json.load(open('$ROOT/data/output/refresh_last.json'));print(d['ts']);fails=[k for k,v in d['steps'].items() if v.startswith('FAIL')];print('FAILED:',fails or 'none')" \
  || echo "no refresh_last.json"
echo "── health ──"
[ -f "$HP" ] && python3 -c "
import json; d=json.load(open('$HP'))
print(d['ts']); print('flags:', len(d.get('flags',[])))
for k,v in d.get('series',{}).items():
    if v['status']!='green': print(' ', v['status'].upper(), k, v.get('notes'))
print('greens:', sum(1 for v in d.get('series',{}).values() if v['status']=='green'))" \
  || echo "no macro_health.json"
