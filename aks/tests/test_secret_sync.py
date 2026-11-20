"""Exercise the secret helper with fake CLIs; never contacts a real cluster."""
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sync-runtime-secret.sh"


class SecretSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.files = self.root / "tmp"
        self.files.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        TMPDIR=str(self.files), TEST_ROOT=str(self.root),
                        KUBE_CONTEXT="test-aks", BETTER_AUTH_SECRET="a" * 64,
                        OPSRABBIT_NODE_ENCRYPTION_KEY="b" * 64)
        self.cli("terraform", """
import sys
assert sys.argv[-3:] == ['output', '-raw', 'database_url']
print('postgresql://user:private-password@private-db/opsrabbit?sslmode=require', end='')
""")
        self.cli("kubectl", """
import json, os, pathlib, stat, sys
root = pathlib.Path(os.environ['TEST_ROOT'])
args = sys.argv[1:]
assert args[:4] == ['--context', 'test-aks', '-n', 'test-namespace']
assert not any('private-password' in a or 'a' * 64 in a or 'b' * 64 in a for a in args)
if 'create' in args:
    data = {}
    for arg in args:
        if arg.startswith('--from-file='):
            p = pathlib.Path(arg.split('=', 1)[1])
            assert stat.S_IMODE(p.stat().st_mode) == 0o600
            data[p.name] = p.read_text()
    print(json.dumps(data))
else:
    assert '--server-side' in args
    (root / 'applied.json').write_text(sys.stdin.read())
    print('secret/test-secret configured')
""")

    def cli(self, name, body):
        import sys
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o755)

    def run_script(self):
        return subprocess.run(["bash", str(SCRIPT), "test-namespace", "test-secret"],
                              env=self.env, text=True, capture_output=True, check=False)

    def test_sync_uses_files_and_cleans_them(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads((self.root / "applied.json").read_text())
        self.assertEqual(data["BETTER_AUTH_SECRET"], "a" * 64)
        self.assertEqual(data["OPSRABBIT_NODE_ENCRYPTION_KEY"], "b" * 64)
        self.assertIn("private-password", data["DATABASE_URL"])
        for secret in data.values():
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(list(self.files.iterdir()), [])

    def test_missing_context_fails_before_secret_creation(self):
        self.env.pop("KUBE_CONTEXT")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "applied.json").exists())

    def test_terraform_failure_cleans_temporary_files(self):
        self.cli("terraform", "import sys\nsys.exit(1)\n")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "applied.json").exists())
        self.assertEqual(list(self.files.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
