#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path


def load_env_file() -> None:
    env_path = Path(os.environ.get("SPLATTOPCONFIG_ENV_FILE", ".env"))
    if not env_path.exists():
        return

    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        os.environ.setdefault(key, value)


def usage() -> None:
    print(f"Usage: {sys.argv[0]} <bot_name> [discord_token|BOT_TOKEN env]")
    sys.exit(1)


def main() -> None:
    load_env_file()

    if len(sys.argv) < 2:
        usage()

    bot_name = sys.argv[1]
    token = sys.argv[2] if len(sys.argv) >= 3 else os.environ.get("BOT_TOKEN")

    if not token:
        print("Provide the Discord token as an argument or set BOT_TOKEN in your environment/.env.")
        sys.exit(1)

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
  BOT_TOKEN: "{token}"
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
        print("🔥 SOPS encryption failed:")
        print(e.stderr.decode())
        secret_file.unlink(missing_ok=True)
    except Exception as e:
        print(f"An error occurred: {e}")
        secret_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
