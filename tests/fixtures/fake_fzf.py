import os
import sys

data = sys.stdin.buffer.read()
if os.environ.get("TEST_FZF_CANCEL"):
    sys.exit(130)
if data:
    sys.stdout.buffer.write(data.split(b"\0", 1)[0] + b"\0")
else:
    sys.exit(1)
