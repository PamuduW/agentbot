import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from src.paths import AgentbotPaths


class BoostEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.paths = AgentbotPaths(
            self.home,
            self.home / ".codex",
            self.home / ".claude",
            self.home / ".cursor",
            config_home=self.home / ".config/agentbot",
            agents_home=self.home / ".agents",
        )
        self.summary = self.home / "summary.md"
        self.summary.write_text("The selected test passed after checking its exit status.")
        self.record = self.home / "record.jsonl"
        self.mode = self.home / "mode"
        self.mode.write_text("success")
        for name in (".boost", ".codex", ".local/share/boost", "fake-bin"):
            (self.home / name).mkdir(parents=True, exist_ok=True)
        (self.home / ".boost/config.toml").write_text(
            'accept_terms=true\nterms_version="fixture"\n'
        )
        (self.home / ".codex/auth.json").write_text('{"fixture":"never print"}')
        self.database = self.home / ".local/share/boost/history.db"
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript(
                "CREATE TABLE tracker_state(key TEXT PRIMARY KEY,value TEXT); INSERT INTO tracker_state VALUES('boost_pro_unlock','private fixture grant'); CREATE TABLE real_history(text TEXT); INSERT INTO real_history VALUES('DO_NOT_COPY_HISTORY');"
            )
            db.commit()
        self.boost = self.home / "fake-bin/boost"
        self.codex = self.home / "fake-bin/codex"
        common = f"""#!{sys.executable}
import json,os,sqlite3,subprocess,sys,time
from pathlib import Path
from unittest import mock
record=Path({str(self.record)!r})
mode=Path({str(self.mode)!r}).read_text()
args=sys.argv[1:]
def log(data):
 with record.open('a') as f:f.write(json.dumps(data)+'\\n')
"""
        self.boost.write_text(
            common
            + """
home=Path(os.environ['HOME']);db=home/'data/boost/history.db'
if args==['insights','status']:
 db.parent.mkdir(parents=True)
 with sqlite3.connect(db) as c:
  c.executescript("CREATE TABLE tracker_state(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE conversations(conversation_id TEXT,title TEXT,first_seen TEXT,last_seen TEXT,tool TEXT,boost_mode TEXT,ended_at TEXT,status TEXT,transcript_path TEXT); CREATE TABLE agent_turns(conversation_id TEXT,turn_id TEXT,user_text TEXT,assistant_text TEXT); CREATE TABLE session_insight_summaries(conversation_id TEXT,status TEXT,summary TEXT,model TEXT,generated_at TEXT,source_last_seen TEXT);")
 raise SystemExit(0)
with sqlite3.connect(db) as c:
 rows=c.execute('SELECT summary FROM session_insight_summaries').fetchall()
 log({'kind':'boost','rows':rows,'tables':[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")], 'home':str(home),'mode':home.stat().st_mode & 511})
prompt='Write lessons from this selected summary: '+rows[0][0]
if mode=='large-prompt':prompt+='x'*17000
if mode=='secret-prompt':prompt+=' password=syntheticsecret123'
with tempfile.TemporaryDirectory(prefix='boost-filtergen-') as outdir:
 log({'kind':'vendor-output','path':outdir,'inside':Path(outdir).is_relative_to(home)})
 output=Path(outdir)/'last.txt'
 argv=['codex','exec','--json','-o',str(output),'-c','features.hooks=false','-c','features.shell=false','-c','sandbox_mode="read-only"','--skip-git-repo-check',prompt]
 if mode=='unsafe-override':argv[1:1]=['--dangerously-bypass-approvals-and-sandbox']
 p=subprocess.run(argv,capture_output=True,text=True)
 if mode=='retry':p=subprocess.run(argv,capture_output=True,text=True)
 if p.returncode:raise SystemExit(p.returncode)
 print('password=syntheticsecret123' if mode=='secret-output' else '# Proposed lessons\\n\\n- Check exit status before claiming success.')
"""
        )
        # tempfile is needed only by the fake vendor command.
        self.boost.write_text(
            self.boost.read_text().replace(
                "import json,os,sqlite3,subprocess,sys,time",
                "import json,os,sqlite3,subprocess,sys,time,tempfile",
            )
        )
        self.codex.write_text(
            common
            + """
if args[:2]==['login','status']:
 print('Logged in using ChatGPT');raise SystemExit(0 if mode!='no-auth' else 1)
if args[:2]==['features','list']:
 for arg in args:
  if arg.startswith('features.'):
   name=arg.split('=')[0].split('.')[1]
   print(name,'stable','true' if mode=='changed-features' and name=='shell_tool' else 'false')
 raise SystemExit(0)
if '--version' in args:
 print('codex-cli fixture');raise SystemExit(0)
if '--help' in args:
 print('codex exec');raise SystemExit(0)
log({'kind':'exec','argv':args,'home':os.environ['HOME'],'env_keys':list(os.environ),'pid':os.getpid()})
if mode=='failure':
 print('password=syntheticsecret123',file=sys.stderr);raise SystemExit(9)
if mode=='timeout':
 child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'])
 log({'kind':'grandchild','pid':child.pid})
 time.sleep(10)
if mode=='unexpected-tool':print(json.dumps({'type':'item.completed','item':{'type':'command_execution'}}))
text=json.dumps({'summary':'fixture','friction':[],'lessons':[]})
Path(args[args.index('-o')+1]).write_text(text)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':30,'cached_input_tokens':10,'output_tokens':8}}))
"""
        )
        self.boost.chmod(0o700)
        self.codex.chmod(0o700)

    def engine(self, **kwargs):
        try:
            module = importlib.import_module("src.boost_engine")
        except ModuleNotFoundError:
            self.fail("The bounded Boost Engine adapter is missing")
        return module.BoostEngine(self.paths, boost=self.boost, codex=self.codex, **kwargs)

    def records(self):
        return (
            [json.loads(line) for line in self.record.read_text().splitlines()]
            if self.record.exists()
            else []
        )

    def test_preview_does_not_launch_processes_or_read_credentials(self):
        self.database.unlink()
        (self.home / ".codex/auth.json").unlink()
        result = self.engine().analyze(self.summary)
        self.assertFalse(result.confirmed)
        self.assertEqual(self.summary.stat().st_size, result.summary_bytes)
        self.assertEqual([], self.records())

    def test_rejects_empty_large_binary_and_credential_input_without_echoing_it(self):
        for content in (b"", b"a" * 8193, b"\xff", b"password=syntheticsecret123", b"a\x00b"):
            with self.subTest(content_length=len(content)):
                self.summary.write_bytes(content)
                with self.assertRaises(ValueError) as error:
                    self.engine().analyze(self.summary, confirmed=True)
                self.assertNotIn("syntheticsecret123", str(error.exception))
        self.assertEqual([], self.records())

    def test_success_isolates_history_and_config_and_reports_native_usage(self):
        before = self.database.read_bytes()
        os.environ["BOOST_ENGINE_TEST_SECRET"] = "do not inherit"
        self.addCleanup(os.environ.pop, "BOOST_ENGINE_TEST_SECRET", None)
        result = self.engine().analyze(self.summary, confirmed=True)
        self.assertTrue(result.confirmed)
        self.assertIn("Proposed lessons", result.proposal)
        self.assertEqual(30, dict(result.usage)["input_tokens"])
        rows = self.records()
        vendor = next(r for r in rows if r["kind"] == "boost")
        native = [r for r in rows if r["kind"] == "exec"]
        self.assertEqual(1, len(native))
        self.assertEqual([[self.summary.read_text()]], vendor["rows"])
        self.assertNotIn("real_history", vendor["tables"])
        self.assertEqual(0o700, vendor["mode"])
        self.assertFalse(Path(vendor["home"]).exists())
        self.assertIn("--ignore-user-config", native[0]["argv"])
        for feature in (
            "hooks",
            "shell_tool",
            "apps",
            "plugins",
            "multi_agent",
            "view_image",
            "image_generation",
        ):
            self.assertIn("features." + feature + "=false", native[0]["argv"])
        self.assertIn("--ephemeral", native[0]["argv"])
        self.assertNotIn("BOOST_ENGINE_TEST_SECRET", native[0]["env_keys"])
        self.assertEqual(before, self.database.read_bytes())

    def test_failure_retry_oversized_prompt_changed_features_and_secret_output_fail_closed(self):
        for mode in (
            "failure",
            "retry",
            "large-prompt",
            "changed-features",
            "secret-output",
            "no-auth",
            "unsafe-override",
            "unexpected-tool",
            "secret-prompt",
        ):
            with self.subTest(mode=mode):
                self.mode.write_text(mode)
                self.record.unlink(missing_ok=True)
                with self.assertRaises(ValueError) as error:
                    self.engine().analyze(self.summary, confirmed=True)
                self.assertNotIn("syntheticsecret123", str(error.exception))
                native = [r for r in self.records() if r["kind"] == "exec"]
                self.assertLessEqual(len(native), 1)
                if mode in (
                    "large-prompt",
                    "changed-features",
                    "no-auth",
                    "unsafe-override",
                    "secret-prompt",
                ):
                    self.assertEqual([], native)
                for row in self.records():
                    if "home" in row:
                        self.assertFalse(Path(row["home"]).exists())

    def test_timeout_stops_the_process_group(self):
        self.mode.write_text("timeout")
        with self.assertRaisesRegex(ValueError, "timed out"):
            self.engine(timeout=0.8).analyze(self.summary, confirmed=True)
        native = [r for r in self.records() if r["kind"] == "exec"]
        self.assertEqual(1, len(native))
        self.assertFalse(Path(native[0]["home"]).exists())

        for row in self.records():
            if "pid" in row:
                process = Path("/proc") / str(row["pid"]) / "stat"
                self.assertTrue(not process.exists() or process.read_text().split()[2] == "Z")

    def test_interrupt_always_cleans_up_the_child_group(self):
        from src.boost_engine import _run

        child = mock.Mock()
        child.pid = 999999
        child.communicate.side_effect = KeyboardInterrupt
        with (
            mock.patch("src.boost_engine.subprocess.Popen", return_value=child),
            mock.patch("src.boost_engine.os.killpg") as kill,
        ):
            with self.assertRaises(KeyboardInterrupt):
                _run(["fixture"], self.home, {}, 90)
        self.assertEqual(2, kill.call_count)
        child.wait.assert_called_once()
        child.stdout.close.assert_called_once()
        child.stderr.close.assert_called_once()

    def test_sigterm_supervisor_does_not_leave_analysis_running(self):
        import signal
        import subprocess
        import time

        pid_file = self.home / "child.pid"
        script = f"import os,time;from pathlib import Path;Path({str(pid_file)!r}).write_text(str(os.getpid()));time.sleep(10)"
        supervisor_code = f"from src.boost_engine import _run;from pathlib import Path;import os;_run([{sys.executable!r},'-c',{script!r}],Path({str(self.home)!r}),dict(os.environ),90)"
        supervisor = subprocess.Popen(
            [sys.executable, "-c", supervisor_code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        child_pid = None
        try:
            for _ in range(100):
                if pid_file.exists():
                    child_pid = int(pid_file.read_text())
                    break
                time.sleep(0.01)
            self.assertIsNotNone(child_pid)
            supervisor.send_signal(signal.SIGTERM)
            supervisor.wait(timeout=3)
            process = Path("/proc") / str(child_pid) / "stat"
            self.assertTrue(
                not process.exists() or process.read_text().split()[2] == "Z",
                "Analysis child survived SIGTERM to its supervisor",
            )
        finally:
            if child_pid is not None:
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if supervisor.poll() is None:
                supervisor.kill()
            supervisor.wait()

    def test_vendor_temporary_files_stay_in_the_isolated_profile(self):
        import shutil

        self.engine().analyze(self.summary, confirmed=True)
        vendor = next(row for row in self.records() if row["kind"] == "vendor-output")
        try:
            self.assertTrue(vendor["inside"])
            self.assertFalse(Path(vendor["path"]).exists())
        finally:
            if Path(vendor["path"]).exists():
                shutil.rmtree(vendor["path"])

    def test_sigterm_during_profile_preflight_removes_the_private_profile(self):
        import shutil
        import signal
        import subprocess
        import time

        ready = self.home / "profile-ready"
        code = f"""from src.boost_engine import BoostEngine
from src.paths import AgentbotPaths
from pathlib import Path
import time
home=Path({str(self.home)!r})
paths=AgentbotPaths(home,home/'.codex',home/'.claude',home/'.cursor')
class PausedEngine(BoostEngine):
 @staticmethod
 def _preflight(codex,profile,env):
  Path({str(ready)!r}).write_text(str(profile))
  time.sleep(10)
PausedEngine(paths,boost=Path({str(self.boost)!r}),codex=Path({str(self.codex)!r})).analyze(Path({str(self.summary)!r}),confirmed=True)
"""
        supervisor = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        profile = None
        try:
            for _ in range(100):
                if ready.exists():
                    profile = Path(ready.read_text())
                    break
                time.sleep(0.01)
            self.assertIsNotNone(profile)
            supervisor.send_signal(signal.SIGTERM)
            supervisor.wait(timeout=3)
            self.assertFalse(profile.exists())
        finally:
            if supervisor.poll() is None:
                supervisor.kill()
            supervisor.wait()
            if profile is not None and profile.exists():
                shutil.rmtree(profile)
