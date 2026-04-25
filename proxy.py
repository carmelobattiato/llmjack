#!/usr/bin/env python3
"""DEPRECATO — usa llmjack.py al posto di proxy.py."""
import subprocess
import sys

print("[!] proxy.py è deprecato — usa: python llmjack.py", flush=True)
result = subprocess.run([sys.executable, "llmjack.py", "--no-wizard"] + sys.argv[1:])
sys.exit(result.returncode)
