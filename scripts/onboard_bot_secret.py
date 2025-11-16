#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path

if len(sys.argv) != 3:
    print(f"Usage: {sys.argv[0]} <bot_name> <discord_token>")
    sys.exit(1)

bot_name = sys.argv[1]
token = sys.argv[2]
bot_ns = f"splattop-bot-{bot_name}"
secret_dir = Path(f"secrets/bots/{bot_name}")
secret_file = secret_dir / "token.enc.yaml"

secret_template = f"""
apiVersion: v1
kind: Secret
metadata:
  name: bot-token
  namespace: {bot_ns}
stringData:
  DISCORD_TOKEN: "{token}"
"""

secret_dir.mkdir(parents=True, exist_ok=True)
secret_file.write_text(secret_template)

try:
    subprocess.run(
        ["sops", "--encrypt", "--in-place", str(secret_file)],
        check=True,
        capture_output=True,
    )
    print(f"✅ Success! Encrypted secret created at: {secret_file}")
except subprocess.CalledProcessError as e:
    print(f"🔥 SOPS encryption failed:")
    print(e.stderr.decode())
    secret_file.unlink(missing_ok=True)
except Exception as e:
    print(f"An error occurred: {e}")
    secret_file.unlink(missing_ok=True)
