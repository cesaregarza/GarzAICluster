"""citrus-redis requires a password from the app-key Secret (CES-1508).

The value lives only in KSOPS; the chart references the key by name and
builds each client URL from $(REDIS_PASSWORD), so no rendered manifest holds
a password or a passwordless Redis URL.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_citrus_redis_ingress_policy import (
    CHART,
    DEV,
    HELM,
    PROD,
    pod_templates,
    render,
)

SECRETS = {"citrus": "citrus-app-key", "citrus-dev": "citrus-dev-app-key"}
PASSWORD_REF = {
    "valueFrom": {
        "secretKeyRef": {"key": "REDIS_PASSWORD", "optional": False},
    },
}
URLS = {
    "CACHE_URL": "redis://:$(REDIS_PASSWORD)@citrus-redis:6379/0",
    "CELERY_BROKER_URL": "redis://:$(REDIS_PASSWORD)@citrus-redis:6379/1",
}
STARTUP_SCRIPT = (
    "umask 077 && "
    "printf 'requirepass %s\\n' \"$REDIS_PASSWORD\" > /tmp/redis-auth.conf && "
    "exec redis-server /tmp/redis-auth.conf "
    "--maxmemory 200mb --maxmemory-policy volatile-lru"
)
# Shell-hostile but harmless: spaces split, glob and quote characters, and
# text that looks like expansions. Synthetic; never a real credential.
DUMMY_PASSWORD = "dum my*pw?[x] $HOME `id` 'q' \"dq\" \\ ;&|<>"
STARTUP_TOKENS = [
    "umask", "077", "&&",
    "printf", "requirepass %s\\n", "$REDIS_PASSWORD", ">", "/tmp/redis-auth.conf", "&&",
    "exec", "redis-server", "/tmp/redis-auth.conf",
    "--maxmemory", "200mb", "--maxmemory-policy", "volatile-lru",
]
PONG_PROBE = ["/bin/sh", "-ec", 'test "$(redis-cli --raw ping)" = PONG']


def run_startup(script, password):
    """Run a Redis start script in a temp dir with redis-server stubbed."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conf, argv_file, bin_dir = root / "redis-auth.conf", root / "argv", root / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "redis-server"
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CITRUS_TEST_ARGV"\n')
        stub.chmod(0o755)
        local = script.replace("/tmp/redis-auth.conf", str(conf))
        if local.count(str(conf)) != 2:
            raise AssertionError("start script no longer writes and reads the config file")
        result = subprocess.run(
            ["sh", "-c", local],
            env={
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "REDIS_PASSWORD": password,
                "CITRUS_TEST_ARGV": str(argv_file),
            },
            cwd=tmp,
            capture_output=True,
            text=True,
            timeout=10,
            preexec_fn=lambda: os.umask(0o022),  # dropping umask 077 must show
        )
        written = conf.read_text() if conf.exists() else None
        mode = stat.S_IMODE(conf.stat().st_mode) if conf.exists() else None
        argv = argv_file.read_text() if argv_file.exists() else ""
        return result, written, mode, argv


def password_ref(secret):
    ref = {"valueFrom": {"secretKeyRef": dict(PASSWORD_REF["valueFrom"]["secretKeyRef"])}}
    ref["valueFrom"]["secretKeyRef"]["name"] = secret
    return ref


@unittest.skipUnless(shutil.which(HELM) or Path(HELM).exists(), "helm is required")
class CitrusRedisAuthTests(unittest.TestCase):
    def renders(self):
        return ((PROD[0], render(*PROD)), (DEV[0], render(*DEV)))

    def test_redis_reads_requirepass_from_a_file_and_probes_authenticate(self):
        for release, documents in self.renders():
            [container] = [
                template for name, template in pod_templates(documents)
                if name == "citrus-redis"
            ][0]["spec"]["containers"]
            with self.subTest(release=release):
                env = {item["name"]: item for item in container["env"]}
                for name in ("REDIS_PASSWORD", "REDISCLI_AUTH"):
                    self.assertEqual(
                        {k: v for k, v in env[name].items() if k != "name"},
                        password_ref(SECRETS[release]),
                    )
                self.assertEqual(container["command"], ["sh", "-c"])
                [script] = container["args"]
                # The whole startup is pinned byte for byte: a later option
                # would override an earlier one, and token-level checks can't
                # tell "$REDIS_PASSWORD" from '$REDIS_PASSWORD'.
                self.assertEqual(script, STARTUP_SCRIPT)
                self.assertEqual(shlex.split(script), STARTUP_TOKENS)
                redis_argv = STARTUP_TOKENS[STARTUP_TOKENS.index("exec") + 1:]
                for option in ("--maxmemory", "--maxmemory-policy"):
                    self.assertEqual(redis_argv.count(option), 1)
                for probe in ("livenessProbe", "readinessProbe"):
                    self.assertEqual(container[probe]["exec"]["command"], PONG_PROBE)

    def test_rendered_startup_writes_only_the_password_file(self):
        # Run the rendered script itself under sh with redis-server stubbed:
        # the config file must hold exactly the password, be 0600, and the
        # password must reach neither output nor any process argument.
        for release, documents in self.renders():
            [container] = [
                template for name, template in pod_templates(documents)
                if name == "citrus-redis"
            ][0]["spec"]["containers"]
            [script] = container["args"]
            with self.subTest(release=release):
                result, written, mode, argv = run_startup(script, DUMMY_PASSWORD)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
                self.assertEqual(written, f"requirepass {DUMMY_PASSWORD}\n")
                self.assertEqual(mode, 0o600)
                self.assertNotIn(DUMMY_PASSWORD, script)
                self.assertNotIn(DUMMY_PASSWORD, argv)
                self.assertEqual(
                    argv.splitlines()[1:],
                    ["--maxmemory", "200mb", "--maxmemory-policy", "volatile-lru"],
                )

    def test_every_redis_client_builds_urls_from_the_secret_password(self):
        for release, documents in self.renders():
            config = next(
                doc for doc in documents if doc["kind"] == "ConfigMap"
            )["data"]
            self.assertNotIn("CACHE_URL", config)
            self.assertNotIn("CELERY_BROKER_URL", config)
            clients = 0
            for name, template in pod_templates(documents):
                for container in template["spec"]["containers"]:
                    env = container.get("env", [])
                    names = [item["name"] for item in env]
                    if "CACHE_URL" not in names:
                        continue
                    clients += 1
                    with self.subTest(release=release, pod=name, container=container["name"]):
                        password = names.index("REDIS_PASSWORD")
                        self.assertEqual(
                            {k: v for k, v in env[password].items() if k != "name"},
                            password_ref(SECRETS[release]),
                        )
                        for url_name, url in URLS.items():
                            index = names.index(url_name)
                            self.assertGreater(index, password)
                            self.assertEqual(env[index]["value"], url)
            self.assertEqual(clients, {"citrus": 5, "citrus-dev": 10}[release])

    def test_no_rendered_value_is_a_passwordless_redis_url(self):
        for release, documents in self.renders():
            for name, template in pod_templates(documents):
                for container in template["spec"]["containers"]:
                    for item in container.get("env", []):
                        value = str(item.get("value", ""))
                        if value.startswith("redis://"):
                            with self.subTest(release=release, pod=name, env=item["name"]):
                                self.assertIn("$(REDIS_PASSWORD)@", value)

    def test_configdata_redis_urls_are_rejected_in_every_mode(self):
        modes = {
            "prod": PROD,
            "dev": DEV,
            "base (no recurring runtime)": ("citrus", "default", "values.yaml"),
        }
        for mode, (release, namespace, *value_files) in modes.items():
            for key in ("CACHE_URL", "CELERY_BROKER_URL"):
                for value in ("", "redis://citrus-redis:6379/1"):
                    command = [
                        HELM, "template", release, str(CHART), "--namespace", namespace,
                        *(arg for name in value_files for arg in ("-f", str(CHART / name))),
                        "--set-string", f"application.configData.{key}={value}",
                    ]
                    result = subprocess.run(command, capture_output=True, text=True)
                    with self.subTest(mode=mode, key=key, value=value):
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(
                            f"{key} comes from citrus.redisClientEnv, not configData",
                            result.stderr,
                        )

if __name__ == "__main__":
    unittest.main()
