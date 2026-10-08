import fcntl
import os
from pathlib import Path
import pty
import select
import subprocess
import tempfile
import termios
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tmux-setup.sh"


class PipeExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        (self.work / "tmp").mkdir()
        self.env = dict(os.environ, HOME=str(self.work), TMPDIR=str(self.work / "tmp"),
                        PATH=f"{self.bin}:{os.environ['PATH']}",
                        TEST_TEMPLATE=str(ROOT / "tmux.conf"),
                        TMUX_TEMPLATE_URL="https://example.invalid/tmux.conf",
                        TMUX_CONF_PATH=str(self.work / ".tmux.conf"))
        self.env.pop("TEST_DOWNLOAD_FAIL", None)
        for name, body in {
            "curl": 'exit_code=${TEST_DOWNLOAD_FAIL:-0}\n'
                    '[ "$exit_code" -eq 0 ] || exit "$exit_code"\n'
                    'printf "%s\\n" "$2" > "$HOME/download-url"\n'
                    'cp "$TEST_TEMPLATE" "$4"\n',
            "git": '[ "$1" != clone ] || mkdir -p "$5/.git"\n',
            "tmux": '[ "$1" != -V ] || echo "tmux test"\n',
        }.items():
            path = self.bin / name
            path.write_text("#!/bin/bash\nset -eu\n" + body)
            path.chmod(0o755)

    def pipe(self, *args):
        return subprocess.run(["bash", "-s", "--", *args], input=SCRIPT.read_text(),
                              cwd=self.work, env=self.env, capture_output=True,
                              text=True, start_new_session=True, timeout=10)

    def test_pipe_install_and_backup(self):
        config = self.work / ".tmux.conf"
        config.write_text("old config\n")
        (self.work / "tmux.conf").write_text("wrong working-directory template\n")
        result = self.pipe("--yes", "--no-reload")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_bytes(), (ROOT / "tmux.conf").read_bytes())
        self.assertEqual(next(self.work.glob(".tmux.conf.backup.*")).read_text(),
                         "old config\n")
        self.assertEqual((self.work / "download-url").read_text().strip(),
                         self.env["TMUX_TEMPLATE_URL"])
        self.assertEqual(list((self.work / "tmp").iterdir()), [])

    def test_local_script_uses_adjacent_template(self):
        result = subprocess.run(["bash", str(SCRIPT), "--dry-run"], cwd=self.work,
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.work / "download-url").exists())
        self.assertFalse((self.work / ".tmux.conf").exists())

    def test_no_terminal_requires_yes(self):
        result = self.pipe()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--yes", result.stderr)
        self.assertFalse((self.work / ".tmux.conf").exists())
        self.assertEqual(list((self.work / "tmp").iterdir()), [])

    def test_failed_download_cleans_up(self):
        self.env["TEST_DOWNLOAD_FAIL"] = "22"
        result = self.pipe("--yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("下载配置模板失败", result.stderr)
        self.assertFalse((self.work / ".tmux.conf").exists())
        self.assertEqual(list((self.work / "tmp").iterdir()), [])

    def test_pipe_confirmation_reads_terminal(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)

        def attach_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        process = subprocess.Popen(["bash", "-c", 'cat "$1" | bash', "test", str(SCRIPT)],
                                   cwd=self.work, env=self.env, stdin=slave,
                                   stdout=slave, stderr=slave, preexec_fn=attach_terminal)
        os.close(slave)
        self.addCleanup(process.wait)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        output = b""
        answered = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                if b"[y/N]" in output and not answered:
                    os.write(master, b"y\n")
                    answered = True
            elif process.poll() is not None:
                break
        self.assertTrue(answered, output.decode())
        self.assertEqual(process.wait(timeout=2), 0, output.decode())
        self.assertEqual((self.work / ".tmux.conf").read_bytes(),
                         (ROOT / "tmux.conf").read_bytes())


if __name__ == "__main__":
    unittest.main()
