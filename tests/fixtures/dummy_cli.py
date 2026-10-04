#!/usr/bin/env python3
"""結合テスト用CLI。実ハーネスやネットワークには接続しない。"""

import json
import os
from pathlib import Path
import sys
import time

request = sys.stdin.read()
info = dict(pid=os.getpid(), pgid=os.getpgrp(), stdin_isatty=sys.stdin.isatty(),
            stdout_isatty=sys.stdout.isatty())
temporary = Path('dummy-cli.json.tmp')
temporary.write_text(json.dumps(info))
temporary.replace('dummy-cli.json')
if 'DUMMY_WAIT' in request:
    time.sleep(30)
if 'DUMMY_FAIL' in request:
    sys.exit(17)
if '--output-last-message' in sys.argv:
    path = Path(sys.argv[sys.argv.index('--output-last-message') + 1])
    path.write_text('DUMMY_OK')
    print(json.dumps({'type': 'item.completed'}), flush=True)
else:
    print(json.dumps({'result': 'DUMMY_OK', 'is_error': False}), flush=True)
