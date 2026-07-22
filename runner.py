# start.py

import subprocess
import sys

# Start tracker.py (hide output)
tracker = subprocess.Popen(
    [sys.executable, "tracker.py"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

# Start analyzer.py (hide output)
analyzer = subprocess.Popen(
    [sys.executable, "analyzer.py"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

# Start trader.py (show output)
trader = subprocess.Popen(
    [sys.executable, "-u", "trader.py"]  # -u = unbuffered output
)

try:
    # Wait until trader.py finishes
    trader.wait()
finally:
    # Stop background scripts when trader exits
    tracker.terminate()
    analyzer.terminate()

    tracker.wait()
    analyzer.wait()
