"""Initialize the single Fly volume, write operator auth, then drop root."""
import os
from pathlib import Path
import re
import sys

runtime = Path('/var/lib/breadcast-studio')
token = os.environ.pop('BREADCAST_OPERATOR_TOKEN', '')
if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):
    raise SystemExit('Set BREADCAST_OPERATOR_TOKEN in media host secrets (32–128 URL-safe characters)')
os.umask(0o077)
runtime.mkdir(parents=True, exist_ok=True)
os.chown(runtime, 10001, 10001)
token_file = runtime / '.operator-token'
temporary = runtime / '.operator-token.new'
fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'w') as output:
    output.write(token+'\n'); output.flush(); os.fsync(output.fileno())
os.chown(temporary, 10001, 10001)
os.replace(temporary, token_file)
os.environ['BREADCAST_OPERATOR_TOKEN_FILE'] = str(token_file)
os.environ['BREADCAST_OPERATOR_AUTH'] = 'token'
os.environ['BREADCAST_RUNTIME'] = str(runtime)
os.initgroups('studio', 10001)
os.setgid(10001)
os.setuid(10001)
os.execv('/usr/local/bin/breadcast-studio', ['breadcast-studio', *(sys.argv[1:] or ['serve'])])
