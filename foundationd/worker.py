"""`python3 -m foundationd.worker <kind>`: run one worker (development and tests; production uses
/usr/libexec/foundationd-worker)."""

import sys

from .workerlib import main

sys.exit(main(sys.argv[1]))
