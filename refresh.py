#!/usr/bin/env python3
import subprocess, json

print("Content-Type: application/json")
print()

try:
    r = subprocess.run(
        ['/usr/local/bin/clima-fetch', '--once'],
        capture_output=True, text=True, timeout=28
    )
    if r.returncode == 0:
        print(json.dumps({'ok': True}))
    else:
        print(json.dumps({'ok': False, 'error': r.stderr[:300]}))
except subprocess.TimeoutExpired:
    print(json.dumps({'ok': False, 'error': 'timeout'}))
except Exception as e:
    print(json.dumps({'ok': False, 'error': str(e)}))
