"""Disposable updater transaction and Win32 fixtures. No production mutex."""
from __future__ import annotations

import hashlib
import ast
import json
import os
import sys
import tempfile
import threading
import types
import subprocess
import time
import ctypes
import zipfile
import unittest
from unittest import mock
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import update_apply as apply
from updates import UpdateController
from test_updates import BundleFixture, ArchiveOpener
from update_package import stage_release


MESSAGES = {code: "Notice שלום " + code for code in apply.MESSAGE_CODES}


class Fixture:
    def __init__(self, root, *, core=None, old_entry=None, new_entry=None):
        self.root = root
        self.install = root / "install"
        helpers = {"app/update_apply.py": (ROOT / "app/update_apply.py").read_bytes(),
                   "app/update_package.py": (ROOT / "app/update_package.py").read_bytes(),
                   "main.py": b"# old entry\n", "old-only.txt": b"retire"}
        if core:
            helpers.update(core)
            helpers["native/fixture.dat"] = b"old locked bytes"
        if old_entry:
            helpers["main.py"] = old_entry
        (root / "old").mkdir()
        self.old = BundleFixture(root / "old", "2.4.0", helpers)
        self.old.install_to(self.install)
        self.config = root / "outside config.json"
        self.config.write_bytes(b'{"saved":"final layout"}')
        self.sentinels = {"config.json": b"personal", "native/libraries/user.dll": b"user",
                          "recordings/keep.mkv": b"movie"}
        for name, data in self.sentinels.items():
            path = self.install / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (root / "new").mkdir()
        self.new = BundleFixture(root / "new", "2.4.1", {**(core or {}),
                                                          **{k: v for k, v in helpers.items() if k.startswith("app/update_")},
                                                          **({"native/fixture.dat": b"new unlocked bytes"} if core else {}),
                                                          "main.py": new_entry or b"# new entry\n", "new-only.txt": b"new"})
        self.transaction = apply.create_transaction(self.install)
        with mock.patch("update_package.urllib.request.build_opener", return_value=ArchiveOpener(self.new.path.read_bytes())):
            self.staged = stage_release(self.new.release, self.transaction, install_dir=self.install)
        self.path = apply.prepare_plan(self.staged, config_path=self.config.name, original_cwd=root, messages=MESSAGES)
        self.plan = apply.load_plan(self.path)

    def unchanged(self, testcase):
        for name, data in self.old.members.items():
            testcase.assertEqual((self.install / name).read_bytes(), data, name)
        for name, data in self.sentinels.items():
            testcase.assertEqual((self.install / name).read_bytes(), data, name)


class Handles:
    def __init__(self, exited=True):
        self.is_exited = exited
        self.closed = False
    def acquire(self, parent_pid=None):
        pass
    def exited(self):
        return self.is_exited
    def close(self):
        self.closed = True


def startup_module():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    early = next(i for i, n in enumerate(tree.body) if isinstance(n, ast.If) and isinstance(n.test, ast.Compare))
    module = types.ModuleType("startup_fixture")
    module.__file__ = str(ROOT / "main.py")
    exec(compile(ast.Module(body=tree.body[:early], type_ignores=[]), "startup_fixture", "exec"), module.__dict__)
    return module


def official_core():
    """Verify fixture bytes against the frozen official release inventory."""
    configured = os.environ.get("NEURALSCREEN_TEST_RUNTIME_ROOT")
    if not configured:
        raise unittest.SkipTest("set test-only NEURALSCREEN_TEST_RUNTIME_ROOT for the disposable official runtime fixture")
    runtime_root = Path(configured)
    archive = runtime_root / "neuralscreen-v2.4.0-full.zip"
    if not archive.exists():
        raise unittest.SkipTest("verified isolated official runtime fixture is unavailable")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != "523ad1ad8fcdffe49050062d20b3dceb2d177be586046b25fcb9468dc91aa65f":
        raise AssertionError("official runtime archive digest changed")
    with zipfile.ZipFile(archive) as source:
        manifest = json.loads(source.read("runtime-manifest.json"))
    records = [r for r in manifest["package"]["files"] if r["path"].startswith("runtime/") and "/Lib/" not in r["path"]]
    result = {}
    for record in records:
        data = (runtime_root / "extracted" / record["path"]).read_bytes()
        if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise AssertionError("isolated official runtime changed: " + record["path"])
        result[record["path"]] = data
    return result


def wait_file(path, process=None, timeout=20):
    deadline = time.monotonic() + timeout
    while not Path(path).exists():
        if process is not None and process.poll() is not None:
            raise AssertionError("fixture process exited before " + str(path) + ": " + str(process.returncode))
        if time.monotonic() >= deadline:
            raise AssertionError("fixture timed out: " + str(path))
        time.sleep(0.02)


def terminate_fixture(process):
    if process is not None and process.poll() is None:
        process.terminate()
        process.wait(timeout=10)


class WindowsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != "win32":
            raise unittest.SkipTest("Windows process and file-lock fixtures")
        cls.core = official_core()

    def test_helper_death_after_authorized_parent_exit_before_first_intent(self):
        import uuid
        with tempfile.TemporaryDirectory(prefix="ns no-move crash ") as temp:
            root = Path(temp)
            fixture = Fixture(root, core=self.core)
            mutex_name = "NeuralScreen_fixture_" + uuid.uuid4().hex
            parent_code = f'''import ctypes, time
from pathlib import Path
k=ctypes.WinDLL('kernel32', use_last_error=True)
k.CreateMutexW.argtypes=[ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
k.CreateMutexW.restype=ctypes.c_void_p
h=k.CreateMutexW(None,False,{mutex_name!r})
Path({str(root / 'parent-ready')!r}).touch()
while not Path({str(root / 'exit-parent')!r}).exists(): time.sleep(.01)
'''
            script = root / "parent.py"
            script.write_text(parent_code, encoding="utf-8")
            parent = helper = None
            try:
                parent = subprocess.Popen([str(fixture.install / "runtime/python.exe"), "-I", "-S", str(script)], close_fds=True)
                wait_file(root / "parent-ready", parent)
                fixture.plan["parent_pid"] = parent.pid
                apply.atomic_json(fixture.path, fixture.plan)
                old_backup = fixture.transaction.parent / "prior-owned-sentinel"
                old_backup.mkdir()
                (old_backup / "keep").write_bytes(b"last good backup")
                wrapper = fixture.transaction / "bootstrap/fixture_crash.py"
                wrapper.write_text(f'''import update_apply as a, time
from pathlib import Path
a.MUTEX_NAME={mutex_name!r}
a.show_notice=lambda text: False
append=a.append_journal
def pause(plan,event,**fields):
    if event=='intent':
        Path({str(root / 'before-intent')!r}).touch()
        while True: time.sleep(.05)
    return append(plan,event,**fields)
a.append_journal=pause
raise SystemExit(a.run_helper(Path({str(fixture.path)!r}),parent_pid={parent.pid}))
''', encoding="utf-8")
                helper = subprocess.Popen([str(fixture.transaction / "bootstrap/runtime/python.exe"), "-I", "-S", str(wrapper)],
                                          cwd=wrapper.parent, env=apply.helper_environment(os.environ), close_fds=True)
                wait_file(fixture.transaction / "armed.json", helper)
                apply.atomic_json(fixture.install / apply.MARKER, apply.identity(fixture.plan))
                apply.write_protocol(fixture.plan, "proceed")
                (root / "exit-parent").touch()
                parent.wait(timeout=10)
                wait_file(root / "before-intent", helper)
                terminate_fixture(helper)
                fixture.unchanged(self)
                self.assertFalse(any(r["event"] == "intent" for r in apply.read_journal(fixture.plan)))
                gate = startup_module()
                gate.__file__ = str(fixture.install / "main.py")
                bindings = gate._startup_bindings
                def unique_bindings():
                    kernel, user = bindings()
                    create = kernel.CreateMutexW
                    kernel.CreateMutexW = lambda a, b, name: create(a, b, mutex_name)
                    return kernel, user
                gate._startup_bindings = unique_bindings
                self.assertEqual(gate._startup_instance_gate(), 0)
                self.assertFalse((fixture.install / apply.MARKER).exists())
                self.assertEqual((old_backup / "keep").read_bytes(), b"last good backup")
                # Close only the uniquely named reference created by this fixture.
                kernel, _ = bindings()
                kernel.CloseHandle(gate._startup_claim)
                gate._startup_claim = None
                # Manual restore follows the same empty/no-move abandonment rule.
                apply.atomic_json(fixture.install / apply.MARKER, apply.identity(fixture.plan))
                with mock.patch.object(apply, "MUTEX_NAME", mutex_name), mock.patch.object(apply, "show_notice", return_value=True):
                    self.assertEqual(apply.run_helper(fixture.path, restore=True), 0)
                fixture.unchanged(self)
                self.assertTrue((fixture.transaction / "bootstrap/runtime/python.exe").exists())
            finally:
                terminate_fixture(helper)
                terminate_fixture(parent)

    def test_real_wait_lock_continuity_early_and_late_targets_and_relaunch(self):
        import uuid
        with tempfile.TemporaryDirectory(prefix="ns win32 space שלום ") as temp:
            root = Path(temp)
            mutex_name = "NeuralScreen_fixture_" + uuid.uuid4().hex
            prefix_tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
            early_if = next(i for i, n in enumerate(prefix_tree.body) if isinstance(n, ast.If) and isinstance(n.test, ast.Compare))
            prefix = ast.unparse(ast.Module(body=prefix_tree.body[:early_if], type_ignores=[]))
            early = (prefix + "\n_real_bindings = _startup_bindings\ndef _startup_bindings():\n"
                     "    kernel, user = _real_bindings()\n    create = kernel.CreateMutexW\n"
                     f"    kernel.CreateMutexW = lambda a,b,n: create(a,b,{mutex_name!r})\n    return kernel, user\n"
                     "raise SystemExit(_startup_instance_gate())\n").encode("utf-8")
            late = f'''import ctypes, os, sys, json
from pathlib import Path
root = Path({str(root)!r})
(root / 'late-import').write_text('observed late import')
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
k.CreateMutexW.restype = ctypes.c_void_p
k.CloseHandle.argtypes = [ctypes.c_void_p]
ctypes.set_last_error(0)
h = k.CreateMutexW(None, False, {mutex_name!r})
error = ctypes.get_last_error()
if error == 183:
    k.CloseHandle(h)
    raise SystemExit(1)
if not h or error:
    raise SystemExit(2)
(root / 'relaunched').write_text(json.dumps({{'cwd_ok': os.path.samefile(os.getcwd(), {str(root)!r}), 'config_ok': os.path.samefile(sys.argv[-1], {str(root / 'outside config.json')!r}), 'ns_ok': os.environ.get('NS_NR_DLL') == 'relative/native.dll', 'path_ok': os.environ.get('PATH') == 'fixture-original-path', 'python_clean': 'PYTHONPATH' not in os.environ and 'PYTHONHOME' not in os.environ, 'transport_clean': 'NEURALSCREEN_UPDATE_PARENT_PATH' not in os.environ}}))
k.CloseHandle(h)
'''.encode("utf-8")
            fixture = Fixture(root, core=self.core, old_entry=early, new_entry=late)
            # These are deliberately unmanaged and must never enter the bootstrap.
            poison = fixture.install / "runtime/Lib/site-packages/sitecustomize.py"
            poison.parent.mkdir(parents=True)
            poison.write_text("import sys\nfrom pathlib import Path\nif 'bootstrap' in Path(sys.executable).parts: raise RuntimeError('bootstrap sentinel executed')")
            (fixture.install / "runtime/unmanaged.dll").write_bytes(b"not executable")
            parent_script = root / "parent.py"
            parent_script.write_text(f'''import ctypes, time
from pathlib import Path
r = Path({str(root)!r})
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
k.CreateMutexW.restype = ctypes.c_void_p
k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
k.CreateFileW.restype = ctypes.c_void_p
k.CloseHandle.argtypes = [ctypes.c_void_p]
mutex = k.CreateMutexW(None, False, {mutex_name!r})
locked = k.CreateFileW({str(fixture.install / 'native/fixture.dat')!r}, 0x80000000, 1, None, 3, 0, None)
assert locked and locked != ctypes.c_void_p(-1).value
(r / 'parent-ready').touch()
while not (r / 'exit-parent').exists(): time.sleep(.01)
k.CloseHandle(locked)
k.CloseHandle(mutex)
''', encoding="utf-8")
            parent = helper = None
            try:
                parent = subprocess.Popen([str(fixture.install / "runtime/python.exe"), "-I", "-S", str(parent_script)], close_fds=True)
                wait_file(root / "parent-ready", parent)
                fixture.plan["parent_pid"] = parent.pid
                apply.atomic_json(fixture.path, fixture.plan)
                wrapper = fixture.transaction / "bootstrap/fixture_helper.py"
                wrapper.write_text(f'''import update_apply as a, time, ctypes, subprocess
from pathlib import Path
r = Path({str(root)!r})
a.MUTEX_NAME = {mutex_name!r}
a.show_notice = lambda text: (r / 'unexpected-notice').write_text('notice') or True
replace = a._replace
def paused(source, destination):
    if Path(destination) == Path({str(fixture.transaction / 'rollback/main.py')!r}):
        (r / 'early-pause').touch()
        while not (r / 'resume-early').exists(): time.sleep(.01)
    replace(source, destination)
    if Path(destination) == Path({str(fixture.install / 'main.py')!r}):
        (r / 'late-pause').touch()
        while not (r / 'resume-late').exists(): time.sleep(.01)
a._replace = paused
launch = a.relaunch
spawn = subprocess.Popen
children = []
def tracked(*args, **kwargs):
    kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process = spawn(*args, **kwargs)
    children.append(process)
    return process
a.subprocess.Popen = tracked
def relaunch(plan, env):
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    ctypes.set_last_error(0)
    h = k.CreateMutexW(None, False, a.MUTEX_NAME)
    assert h and ctypes.get_last_error() == 0, 'helper did not release before relaunch'
    k.CloseHandle(h)
    launch(plan, env)
    out, err = children[-1].communicate(timeout=10)
    assert children[-1].returncode == 0, (children[-1].returncode, out, err)
a.relaunch = relaunch
assert 'site' not in __import__('sys').modules
raise SystemExit(a.run_helper(Path({str(fixture.path)!r}), parent_pid={parent.pid}))
''', encoding="utf-8")
                env = dict(os.environ, PATH="fixture-original-path", NS_NR_DLL="relative/native.dll", PYTHONPATH=str(poison.parent), PYTHONHOME="polluted")
                env = apply.helper_environment(env)
                helper = subprocess.Popen([str(fixture.transaction / "bootstrap/runtime/python.exe"), "-I", "-S", str(wrapper)],
                                          cwd=fixture.transaction / "bootstrap", env=env, close_fds=True)
                wait_file(fixture.transaction / "armed.json", helper)
                self.assertFalse((fixture.transaction / "bootstrap/runtime/unmanaged.dll").exists())
                self.assertFalse((fixture.transaction / "bootstrap/runtime/Lib").exists())
                # The parent actually denies deletion, independently of updater mocks.
                with self.assertRaises(PermissionError):
                    os.replace(fixture.install / "native/fixture.dat", root / "should-not-move")
                apply.atomic_json(fixture.install / apply.MARKER, apply.identity(fixture.plan))
                apply.write_protocol(fixture.plan, "proceed")
                time.sleep(.1)
                self.assertFalse((root / "early-pause").exists(), "must wait for exact parent's exit")
                (root / "exit-parent").touch()
                parent.wait(timeout=10)
                wait_file(root / "early-pause", helper)
                early_process = subprocess.run([str(fixture.install / "runtime/python.exe"), "-I", "-S", str(fixture.install / "main.py")], timeout=10)
                self.assertEqual(early_process.returncode, 1)
                self.assertFalse((root / "relaunched").exists())
                (root / "resume-early").touch()
                wait_file(root / "late-pause", helper)
                late_process = subprocess.run([str(fixture.install / "runtime/python.exe"), "-I", "-S", str(fixture.install / "main.py")], timeout=10)
                self.assertEqual(late_process.returncode, 1)
                self.assertTrue((root / "late-import").exists())
                self.assertFalse((root / "relaunched").exists())
                (root / "resume-late").touch()
                self.assertEqual(helper.wait(timeout=20), 0)
                wait_file(root / "relaunched")
                flags = json.loads((root / "relaunched").read_text())
                self.assertTrue(all(flags.values()), flags)
                self.assertEqual((fixture.install / "native/fixture.dat").read_bytes(), b"new unlocked bytes")
                self.assertFalse((root / "unexpected-notice").exists())
                self.assertTrue(poison.exists())
            finally:
                terminate_fixture(helper)
                terminate_fixture(parent)


class TransactionTests(unittest.TestCase):
    def test_ready_snapshot_survives_cached_archive_removal_before_poll(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            controller = UpdateController("2.4.0", fixture.install)
            with mock.patch("updates.check_latest", return_value=fixture.new.release):
                self.assertTrue(controller.check())
                self.wait_controller(controller, "available")
            transaction = controller.create_download_transaction()
            with mock.patch("update_package.urllib.request.build_opener", return_value=ArchiveOpener(fixture.new.path.read_bytes())):
                self.assertTrue(controller.download(transaction))
                controller._thread.join(timeout=5)
                self.assertFalse(controller._thread.is_alive())
            (transaction / "release.zip").unlink()
            state = controller.poll()
            self.assertEqual(state.phase, "ready")
            self.assertEqual(state.received_bytes, fixture.new.release.size)
            self.assertEqual(state.total_bytes, fixture.new.release.size)
            self.assertTrue(state.can_install)
            self.assertIsNone(state.error_code)
            fixture.unchanged(self)

    def test_cancelled_arm_worker_cannot_reassert_a_late_ack(self):
        for action in ("cancel", "close"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                controller = UpdateController("2.4.0", fixture.install)
                with mock.patch("updates.check_latest", return_value=fixture.new.release):
                    controller.check()
                    self.wait_controller(controller, "available")
                with mock.patch("update_package.urllib.request.build_opener", return_value=ArchiveOpener(fixture.new.path.read_bytes())):
                    controller.download()
                    self.wait_controller(controller, "ready")
                observed = threading.Event()
                release = threading.Event()
                plans = []
                def spawn(argv, **kwargs):
                    plan = apply.load_plan(Path(argv[argv.index("--plan") + 1]))
                    plans.append(plan)
                    (Path(plan["bootstrap_dir"]).parent / "journal.jsonl").touch()
                    apply.write_protocol(plan, "armed")
                    return types.SimpleNamespace(poll=lambda: None)
                real = apply.protocol
                def delayed(plan, name):
                    result = real(plan, name)
                    if name == "armed":
                        observed.set()
                        if not release.wait(timeout=5):
                            raise AssertionError("fixture ACK release timed out")
                    return result
                with mock.patch("updates.subprocess.Popen", side_effect=spawn), mock.patch.object(apply, "protocol", side_effect=delayed):
                    self.assertTrue(controller.arm_install(config_path=fixture.config, original_cwd=Path(temp), messages=MESSAGES))
                    try:
                        self.assertTrue(observed.wait(timeout=5))
                        if action == "cancel":
                            self.assertTrue(controller.cancel_install())
                        else:
                            controller.close()
                        release.set()
                        controller._thread.join(timeout=5)
                        self.assertFalse(controller._thread.is_alive())
                        self.assertFalse(controller.poll_armed())
                        self.assertEqual(controller.poll().phase, "error")
                        self.assertFalse(controller.proceed_install())
                    finally:
                        release.set()
                self.assertTrue(real(plans[0], "cancel"))
                fixture.unchanged(self)
                self.assertFalse(any(record["event"] == "intent" for record in apply.read_journal(plans[0])))

    def test_abandoned_mutex_is_released_before_rejecting_mutation(self):
        api = types.SimpleNamespace(**{name: mock.Mock(return_value=1) for name in
                                      ("CreateMutexW", "OpenProcess", "WaitForSingleObject", "ReleaseMutex", "CloseHandle")})
        api.CreateMutexW.return_value = 0x1234
        api.WaitForSingleObject.return_value = 0x80
        with mock.patch.object(ctypes, "WinDLL", return_value=api), mock.patch.object(ctypes, "get_last_error", return_value=183):
            handles = apply.WindowsHandles()
            try:
                with self.assertRaises(apply.UpdateError):
                    handles.acquire(os.getpid())
            finally:
                handles.close()
        api.ReleaseMutex.assert_called_once_with(0x1234)
        api.CloseHandle.assert_called_once_with(0x1234)

    def test_early_recovery_notice_releases_claim_before_native_notice(self):
        gate = startup_module()
        closed = []
        kernel = types.SimpleNamespace(CreateMutexW=lambda *args: 0x1234, CloseHandle=closed.append)
        def notice(*args):
            self.assertEqual(closed, [0x1234])
            self.assertEqual(args, (None, "recovery שלום", "NeuralScreen", 0x00010000))
            return 0
        user = types.SimpleNamespace(MessageBoxW=notice)
        with mock.patch.object(gate, "_startup_bindings", return_value=(kernel, user)), mock.patch.object(gate, "_startup_update_guard", return_value="recovery שלום"), mock.patch.object(ctypes, "get_last_error", return_value=0):
            self.assertEqual(gate._startup_instance_gate(), 1)
        self.assertIsNone(gate._startup_claim)

    def test_controller_ack_and_authorization_write_failures_cancel_without_moves(self):
        for failure in ("ack", "marker", "proceed", "lost_ack", "dead_helper"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                controller = UpdateController("2.4.0", fixture.install)
                with mock.patch("updates.check_latest", return_value=fixture.new.release):
                    controller.check()
                    self.wait_controller(controller, "available")
                with mock.patch("update_package.urllib.request.build_opener", return_value=ArchiveOpener(fixture.new.path.read_bytes())):
                    controller.download()
                    self.wait_controller(controller, "ready")
                plans = []
                processes = []
                def spawn(argv, **kwargs):
                    plan = apply.load_plan(Path(argv[argv.index("--plan") + 1]))
                    plans.append(plan)
                    (Path(plan["bootstrap_dir"]).parent / "journal.jsonl").touch()
                    record = apply.identity(plan)
                    if failure == "ack":
                        record["transaction_id"] = "0" * 32
                    apply.atomic_json(Path(plan["bootstrap_dir"]).parent / "armed.json", record)
                    process = types.SimpleNamespace(returncode=None)
                    process.poll = lambda: process.returncode
                    processes.append(process)
                    return process
                with mock.patch("updates.subprocess.Popen", side_effect=spawn):
                    self.assertTrue(controller.arm_install(config_path=fixture.config, original_cwd=Path(temp), messages=MESSAGES))
                    if failure == "ack":
                        self.wait_controller(controller, "error")
                    else:
                        deadline = time.monotonic() + 10
                        while not controller.poll_armed():
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(.01)
                        if failure == "lost_ack":
                            (Path(plans[0]["bootstrap_dir"]).parent / "armed.json").unlink()
                        elif failure == "dead_helper":
                            processes[0].returncode = 0
                        real = apply.atomic_json
                        def write(path, value):
                            if Path(path).name == (apply.MARKER if failure == "marker" else "proceed.json"):
                                raise OSError("fixture write failure")
                            return real(path, value)
                        with mock.patch.object(apply, "atomic_json", side_effect=write):
                            self.assertFalse(controller.proceed_install())
                        state = controller.poll()
                        self.assertEqual(state.phase, "error")
                        self.assertEqual(state.error_code, "prepare_failed")
                self.assertTrue(apply.protocol(plans[0], "cancel"), "all failed arming/authorization paths must revoke explicitly")
                fixture.unchanged(self)
                self.assertFalse(any(r["event"] == "intent" for r in apply.read_journal(plans[0])))

    def test_invalid_protocol_identity_never_authorizes_moves(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            original = apply.write_protocol
            def armed(plan, name):
                original(plan, name)
                if name == "armed":
                    apply.atomic_json(fixture.install / apply.MARKER, apply.identity(plan))
                    wrong = {**apply.identity(plan), "transaction_id": "0" * 32}
                    apply.atomic_json(fixture.transaction / "proceed.json", wrong)
            handles = Handles()
            with mock.patch.object(apply, "write_protocol", side_effect=armed), mock.patch.object(apply, "show_notice", side_effect=lambda text: self.assertTrue(handles.closed) or True), mock.patch.object(apply, "relaunch") as launch:
                self.assertEqual(apply.run_helper(fixture.path, parent_pid=os.getpid(), handles=handles), 1)
            fixture.unchanged(self)
            launch.assert_not_called()
            self.assertFalse(any(r["event"] == "intent" for r in apply.read_journal(fixture.plan)))

    def test_native_notice_is_unicode_typed_and_has_expected_caption_and_flags(self):
        box = mock.Mock(return_value=1)
        with mock.patch.object(ctypes, "WinDLL", return_value=types.SimpleNamespace(MessageBoxW=box)):
            self.assertTrue(apply.show_notice("שלום"))
        box.assert_called_once_with(None, "שלום", "NeuralScreen", 0x00010000)
        self.assertEqual(box.restype, ctypes.c_int)
        self.assertEqual(len(box.argtypes), 4)

    def test_environment_conflict_and_child_only_transport_remain_in_memory(self):
        parent = {"PATH": "private original search", "NS_NR_DLL": "relative/native.dll", "NS_GPU_PRIORITY": "high", "PYTHONHOME": "polluted", "PYTHONPATH": "polluted"}
        helper = apply.helper_environment(parent)
        self.assertNotIn("PYTHONHOME", helper)
        self.assertNotIn("PYTHONPATH", helper)
        self.assertNotEqual(helper["PATH"], parent["PATH"])
        relaunched = apply.relaunch_environment(helper)
        self.assertEqual(relaunched, {k: v for k, v in parent.items() if not k.startswith("PYTHON")})
        with self.assertRaises(apply.UpdateError) as error:
            apply.helper_environment({**parent, apply.TRANSPORT: "already present"})
        self.assertEqual(error.exception.code, "environment_conflict")
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            self.assertNotIn(b"private original search", fixture.path.read_bytes())
            self.assertNotIn(b"relative/native.dll", fixture.path.read_bytes())

    def test_sharing_violations_retry_bounded_and_other_failures_do_not_retry(self):
        blocked = OSError("sharing")
        blocked.winerror = 32
        with mock.patch.object(apply.os, "replace", side_effect=[blocked, None]) as replace, mock.patch.object(apply.time, "sleep"):
            apply._replace(Path("fixture-source"), Path("fixture-target"))
            self.assertEqual(replace.call_count, 2)
        with mock.patch.object(apply.os, "replace", side_effect=blocked), mock.patch.object(apply.time, "monotonic", side_effect=[0, 11]), mock.patch.object(apply.time, "sleep") as sleep:
            with self.assertRaises(OSError):
                apply._replace(Path("fixture-source"), Path("fixture-target"))
            sleep.assert_not_called()

    def test_no_move_cleanup_can_resume_after_interruption(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            (fixture.transaction / "journal.jsonl").touch()
            apply.atomic_json(fixture.install / apply.MARKER, apply.identity(fixture.plan))
            apply.write_protocol(fixture.plan, "armed")
            guard = startup_module()
            real = Path.unlink
            def fail_once(path, *args, **kwargs):
                if path.name == apply.MARKER:
                    raise OSError("fixture interruption")
                return real(path, *args, **kwargs)
            with mock.patch.object(Path, "unlink", fail_once):
                self.assertIsInstance(guard._startup_update_guard(fixture.install), str)
            self.assertIsNone(guard._startup_update_guard(fixture.install))
            self.assertFalse((fixture.install / apply.MARKER).exists())
            fixture.unchanged(self)

    def test_corrupt_config_snapshot_never_overwrites_newer_user_config(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            (fixture.transaction / "journal.jsonl").touch()
            apply.atomic_json(fixture.install / apply.MARKER, apply.identity(fixture.plan))
            apply.apply_payload(fixture.plan)
            fixture.config.write_bytes(b"newer config")
            (fixture.transaction / "config-snapshots/before").write_bytes(b"partial")
            with self.assertRaises(apply.UpdateError):
                apply.restore_payload(fixture.plan, manual=True)
            self.assertEqual(fixture.config.read_bytes(), b"newer config")

    def test_failed_manual_restore_never_restarts_or_clears_recovery(self):
        for failure in ("corrupt_snapshot", "config_write"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                before = fixture.config.read_bytes()
                (fixture.transaction / "journal.jsonl").touch()
                apply.atomic_json(fixture.install / apply.MARKER, apply.identity(fixture.plan))
                apply.apply_payload(fixture.plan)
                fixture.config.write_bytes(b"new schema settings")
                snapshot = fixture.transaction / "config-snapshots/before"
                if failure == "corrupt_snapshot":
                    snapshot.write_bytes(b"partial")
                previous = (fixture.transaction.parent / "previous.json").read_bytes()
                replace = apply.os.replace
                def fail_config_write(source, target):
                    if failure == "config_write" and Path(target) == fixture.config:
                        raise PermissionError("fixture config write denied")
                    return replace(source, target)
                handles = Handles()
                notices = []
                def notice(text):
                    notices.append(text)
                    return True
                with mock.patch.object(apply.os, "replace", side_effect=fail_config_write), mock.patch.object(apply.subprocess, "Popen") as launch, mock.patch.object(apply, "show_notice", side_effect=notice):
                    self.assertEqual(apply.run_helper(fixture.path, restore=True, handles=handles, env={}), 1)
                self.assertFalse(launch.called, "manual restore must never relaunch")
                self.assertTrue(handles.closed)
                self.assertEqual(notices, [MESSAGES["recovery_required"] + "\n" + apply.restore_command(fixture.path)])
                self.assertEqual(apply.read_json(fixture.install / apply.MARKER), apply.identity(fixture.plan))
                self.assertEqual(fixture.config.read_bytes(), b"new schema settings")
                self.assertEqual((fixture.transaction.parent / "previous.json").read_bytes(), previous)
                apply.verify_records(fixture.transaction / "rollback", apply._inventory(fixture.plan["old_inventory"]))
                for name, data in fixture.sentinels.items():
                    self.assertEqual((fixture.install / name).read_bytes(), data)
                snapshot.write_bytes(before)
                with mock.patch.object(apply.subprocess, "Popen") as launch, mock.patch.object(apply, "show_notice", return_value=True):
                    self.assertEqual(apply.run_helper(fixture.path, restore=True, handles=Handles(), env={}), 0)
                self.assertFalse(launch.called, "manual restore must never relaunch")
                fixture.unchanged(self)
                self.assertEqual(fixture.config.read_bytes(), before)
                self.assertFalse((fixture.install / apply.MARKER).exists())

    def test_failed_manual_restore_retains_marker_and_backup_for_mixed_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            (fixture.transaction / "journal.jsonl").touch()
            apply.atomic_json(fixture.install / apply.MARKER, apply.identity(fixture.plan))
            apply.apply_payload(fixture.plan)
            with mock.patch.object(apply, "_replace", side_effect=OSError("fixture locked restore")):
                with self.assertRaises(OSError):
                    apply.restore_payload(fixture.plan, manual=True)
            self.assertTrue((fixture.install / apply.MARKER).exists(), "partial manual recovery must exclude startup")
            apply.verify_records(fixture.transaction / "rollback", apply._inventory(fixture.plan["old_inventory"]))
            apply.restore_payload(fixture.plan, manual=True)
            fixture.unchanged(self)

    def test_controller_retries_only_after_cancelled_helper_settles_and_uses_new_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            controller = UpdateController("2.4.0", fixture.install)
            processes = []
            def start(*args, **kwargs):
                argv = args[0]
                path = Path(argv[argv.index("--plan") + 1])
                plan = apply.load_plan(path)
                (path.parent / "journal.jsonl").touch()
                apply.write_protocol(plan, "armed")
                process = types.SimpleNamespace(returncode=None, poll=lambda: process.returncode, plan=plan)
                processes.append(process)
                return process
            def ready():
                with mock.patch("updates.check_latest", return_value=fixture.new.release):
                    self.assertTrue(controller.check())
                    self.wait_controller(controller, "available")
                with mock.patch("update_package.urllib.request.build_opener", return_value=ArchiveOpener(fixture.new.path.read_bytes())):
                    self.assertTrue(controller.download())
                    self.wait_controller(controller, "ready")
            ready()
            with mock.patch("updates.subprocess.Popen", side_effect=start):
                self.assertTrue(controller.arm_install(config_path=fixture.config, original_cwd=Path(temp), messages=MESSAGES))
                deadline = time.monotonic() + 10
                while not controller.poll_armed():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                first = processes[0]
                self.assertFalse(controller.check(), "armed transaction retains ownership after worker done")
                self.assertFalse(controller.download())
                self.assertTrue(controller.cancel_install())
                cancelled = controller.poll()
                self.assertEqual(cancelled.phase, "error", "cancelling an acknowledged arm must leave Preparing")
                self.assertEqual(cancelled.error_code, "prepare_failed")
                self.assertFalse(controller.poll_armed())
                self.assertFalse(controller.check(), "cancellation must settle before new work")
                apply.abandon_no_moves(first.plan)
                apply.write_protocol(first.plan, "cancelled")
                first.returncode = 0
                self.assertEqual(controller.poll().phase, "error")
                ready()
                self.assertTrue(controller.arm_install(config_path=fixture.config, original_cwd=Path(temp), messages=MESSAGES))
                deadline = time.monotonic() + 10
                while not controller.poll_armed():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                self.assertNotEqual(first.plan["transaction_id"], processes[1].plan["transaction_id"])
                self.assertTrue(controller.proceed_install())
                self.assertFalse(controller.check())
                authorized = controller.poll()
                controller.close()
                self.assertEqual(controller.poll(), authorized)
                self.assertTrue(apply.protocol(processes[1].plan, "proceed"))
                self.assertFalse(apply.protocol(processes[1].plan, "cancel"), "authorized normal teardown must not cancel")

    def wait_controller(self, controller, phase):
        deadline = time.monotonic() + 10
        while controller.poll().phase != phase:
            self.assertLess(time.monotonic(), deadline, controller.poll())
            time.sleep(.01)

    def test_manual_restore_without_exclusion_makes_zero_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            (fixture.transaction / "journal.jsonl").touch()
            apply.atomic_json(fixture.install / apply.MARKER, apply.identity(fixture.plan))
            apply.apply_payload(fixture.plan)
            handles = Handles()
            handles.acquire = mock.Mock(side_effect=apply.UpdateError("recovery_required"))
            with mock.patch.object(apply, "show_notice", return_value=True), mock.patch.object(apply, "relaunch"):
                self.assertEqual(apply.run_helper(fixture.path, restore=True, handles=handles), 1)
            self.assertEqual((fixture.install / "main.py").read_bytes(), b"# new entry\n")
            self.assertTrue(handles.closed)

    def test_replayed_helper_never_restores_a_successful_update(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            (fixture.transaction / "journal.jsonl").touch()
            apply.atomic_json(fixture.install / apply.MARKER, apply.identity(fixture.plan))
            apply.apply_payload(fixture.plan)
            with mock.patch.object(apply, "show_notice", return_value=True), mock.patch.object(apply, "relaunch"):
                self.assertEqual(apply.run_helper(fixture.path, parent_pid=os.getpid(), handles=Handles()), 1)
            self.assertEqual((fixture.install / "main.py").read_bytes(), b"# new entry\n")

    def test_invalid_bootstrap_and_plan_emit_notice_without_mutation(self):
        for corrupt in ("plan", "bootstrap"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                apply.atomic_json(fixture.install / apply.MARKER, apply.identity(fixture.plan))
                if corrupt == "plan":
                    fixture.path.write_bytes(b"{bad")
                else:
                    (fixture.transaction / "bootstrap/update_apply.py").write_bytes(b"changed")
                with mock.patch.object(apply, "show_notice", return_value=True) as notice, mock.patch.object(apply, "relaunch") as launch:
                    self.assertEqual(apply.run_helper(fixture.path, parent_pid=os.getpid(), handles=Handles()), 1)
                self.assertEqual(notice.call_count, 1)
                launch.assert_not_called()
                fixture.unchanged(self)
                self.assertTrue((fixture.install / apply.MARKER).exists())

    def test_cancel_after_first_intent_before_first_move_still_wins(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            (fixture.transaction / "journal.jsonl").touch()
            apply.atomic_json(fixture.install / apply.MARKER, apply.identity(fixture.plan))
            original = apply.append_journal
            def append(plan, event, **fields):
                original(plan, event, **fields)
                if event == "intent":
                    apply.write_protocol(plan, "cancel")
            with mock.patch.object(apply, "append_journal", side_effect=append), mock.patch.object(apply, "_replace", wraps=apply._replace) as move:
                self.assertFalse(apply.apply_payload(fixture.plan))
            self.assertTrue(all(Path(call.args[1]).parent == fixture.transaction / "config-snapshots" for call in move.call_args_list))
            fixture.unchanged(self)

    def test_cancel_conflicting_proceed_wins_even_after_parent_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            original = apply.write_protocol
            def armed(plan, name):
                original(plan, name)
                if name == "armed":
                    apply.atomic_json(fixture.install / apply.MARKER, apply.identity(plan))
                    original(plan, "proceed")
                    original(plan, "cancel")
            with mock.patch.object(apply, "write_protocol", side_effect=armed), mock.patch.object(apply, "relaunch") as launch:
                self.assertEqual(apply.run_helper(fixture.path, parent_pid=os.getpid(), handles=Handles()), 0)
            fixture.unchanged(self)
            launch.assert_not_called()
            self.assertFalse((fixture.install / apply.MARKER).exists())

    def test_timeouts_never_move_and_release_handles_before_localized_notice(self):
        for proceed in (False, True):
            with self.subTest(proceed=proceed), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                handles = Handles(exited=False)
                original = apply.write_protocol
                def armed(plan, name):
                    original(plan, name)
                    if name == "armed" and proceed:
                        apply.atomic_json(fixture.install / apply.MARKER, apply.identity(plan))
                        original(plan, "proceed")
                notices = []
                def notice(text):
                    self.assertTrue(handles.closed)
                    notices.append(text)
                    return True
                with mock.patch.object(apply, "write_protocol", side_effect=armed), mock.patch.object(apply, "show_notice", side_effect=notice), mock.patch.object(apply, "relaunch") as launch:
                    self.assertEqual(apply.run_helper(fixture.path, parent_pid=os.getpid(), handles=handles, arm_timeout=0, shutdown_timeout=0), int(proceed))
                fixture.unchanged(self)
                launch.assert_not_called()
                self.assertEqual(notices, [MESSAGES["shutdown_timeout"]] if proceed else [])

    def test_failure_before_and_after_every_move_restores_exact_old_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            prototype = Fixture(Path(temp))
            moves = 1 + len(prototype.plan["old_inventory"]) + len(prototype.plan["new_inventory"])
        for boundary in range(1, moves + 1):
            for after in (False, True):
                with self.subTest(boundary=boundary, after=after), tempfile.TemporaryDirectory() as temp:
                    fixture = Fixture(Path(temp))
                    (fixture.transaction / "journal.jsonl").touch()
                    apply.atomic_json(fixture.install / apply.MARKER, apply.identity(fixture.plan))
                    replace = apply._replace
                    count = 0
                    def fail(source, target):
                        nonlocal count
                        count += 1
                        if count == boundary and not after:
                            raise OSError("fixture move failure")
                        replace(source, target)
                        if count == boundary and after:
                            raise OSError("fixture move failure")
                    with mock.patch.object(apply, "_replace", side_effect=fail):
                        with self.assertRaises(OSError):
                            apply.apply_payload(fixture.plan)
                    apply.restore_payload(fixture.plan)
                    apply.restore_payload(fixture.plan)
                    fixture.unchanged(self)
                    self.assertFalse((fixture.install / "new-only.txt").exists())

    def test_manual_restore_preserves_newer_config_and_last_recovery_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            before = fixture.config.read_bytes()
            (fixture.transaction / "journal.jsonl").touch()
            apply.atomic_json(fixture.install / apply.MARKER, apply.identity(fixture.plan))
            apply.apply_payload(fixture.plan)
            fixture.config.write_bytes(b"new schema settings")
            apply.restore_payload(fixture.plan, manual=True)
            apply.restore_payload(fixture.plan, manual=True)
            fixture.unchanged(self)
            self.assertEqual(fixture.config.read_bytes(), before)
            self.assertEqual((fixture.transaction / "config-snapshots/after").read_bytes(), b"new schema settings")
            apply.verify_records(fixture.transaction / "rollback", apply._inventory(fixture.plan["old_inventory"]))

    def test_no_move_guard_abandons_but_uncertain_or_intent_journals_preserve_everything(self):
        guard = startup_module()
        for journal in (b"", None, b"{", "intent", "committed"):
            with self.subTest(journal=journal), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                marker = fixture.install / apply.MARKER
                apply.atomic_json(marker, apply.identity(fixture.plan))
                if journal is not None:
                    (fixture.transaction / "journal.jsonl").write_bytes(b"" if journal in ("intent", "committed") else journal)
                if journal in ("intent", "committed"):
                    apply.append_journal(fixture.plan, journal, **({"path": "main.py"} if journal == "intent" else {}))
                before = {str(p): p.read_bytes() for p in Path(temp).rglob("*") if p.is_file()}
                notice = guard._startup_update_guard(fixture.install)
                if journal == b"":
                    self.assertIsNone(notice)
                    self.assertFalse(marker.exists())
                    self.assertEqual(apply.read_journal(fixture.plan)[-1]["event"], "abandoned")
                    self.assertTrue(fixture.path.exists())
                else:
                    self.assertIsInstance(notice, str)
                    self.assertIn("--restore", notice)
                    self.assertEqual(before, {str(p): p.read_bytes() for p in Path(temp).rglob("*") if p.is_file()})
                fixture.unchanged(self)

    def test_corrupt_plan_and_outside_marker_use_safe_fallback_without_cleanup(self):
        guard = startup_module()
        for corrupt in ("plan", "marker"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(Path(temp))
                marker = apply.identity(fixture.plan)
                if corrupt == "plan":
                    fixture.path.write_bytes(b"{bad")
                else:
                    marker["plan_path"] = str(Path(temp) / "outside.json")
                apply.atomic_json(fixture.install / apply.MARKER, marker)
                before = {str(p): p.read_bytes() for p in Path(temp).rglob("*") if p.is_file()}
                notice = guard._startup_update_guard(fixture.install)
                self.assertIn("cannot safely start", notice)
                self.assertEqual("--restore" in notice, corrupt == "plan")
                self.assertIn(str(apply.control_dir_for(fixture.install)), notice)
                self.assertEqual(before, {str(p): p.read_bytes() for p in Path(temp).rglob("*") if p.is_file()})

    def test_scripted_gate_precedes_bytecode_and_mutable_imports(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        early = next(n for n in tree.body if isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                     and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in n.test.comparators))
        calls = [(n.lineno, n.func.id) for n in ast.walk(early) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        gate = next((line for line, name in calls if name == "_startup_instance_gate"), None)
        self.assertIsNotNone(gate, "scripted startup must claim before mutable code")
        drop = next(line for line, name in calls if name == "_drop_stale_bytecode")
        self.assertLess(gate, drop)
        mutable = next(n.lineno for n in tree.body if isinstance(n, ast.Import) and any(a.name == "cv2" for a in n.names))
        self.assertLess(gate, mutable)

    def test_second_success_rotates_only_prior_owned_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            (fixture.transaction / "journal.jsonl").touch()
            apply.atomic_json(fixture.install / apply.MARKER, apply.identity(fixture.plan))
            apply.apply_payload(fixture.plan)
            unrelated = fixture.transaction.parent / "do-not-delete"
            unrelated.mkdir()
            (unrelated / "keep").write_bytes(b"user")
            next_root = Path(temp) / "next"
            next_root.mkdir()
            following = BundleFixture(next_root, "2.4.2", {"main.py": b"# third entry\n"})
            txn = apply.create_transaction(fixture.install)
            with mock.patch("update_package.urllib.request.build_opener", return_value=ArchiveOpener(following.path.read_bytes())):
                staged = stage_release(following.release, txn, install_dir=fixture.install)
            path = apply.prepare_plan(staged, config_path=fixture.config, original_cwd=Path(temp), messages=MESSAGES)
            plan = apply.load_plan(path)
            (txn / "journal.jsonl").touch()
            apply.atomic_json(fixture.install / apply.MARKER, apply.identity(plan))
            apply.apply_payload(plan)
            self.assertFalse(fixture.transaction.exists(), "only the older owned recoverable backup rotates after success")
            self.assertTrue((txn / "rollback/main.py").exists())
            self.assertEqual((unrelated / "keep").read_bytes(), b"user")
            apply.restore_payload(plan, manual=True)
            for name, data in fixture.new.members.items():
                self.assertEqual((fixture.install / name).read_bytes(), data, name)
            self.assertTrue((txn / "rollback/main.py").exists(), "manual restore retains the last recovery copy")

    def test_authorized_update_keeps_backup_and_entrypoint_last(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            original = apply.write_protocol
            def armed(plan, name):
                original(plan, name)
                if name == "armed":
                    apply.atomic_json(fixture.install / apply.MARKER, apply.identity(plan))
                    original(plan, "proceed")
            handles = Handles()
            with mock.patch.object(apply, "write_protocol", side_effect=armed), mock.patch.object(apply, "relaunch") as launch:
                self.assertEqual(apply.run_helper(fixture.path, parent_pid=os.getpid(), handles=handles), 0)
            for name, data in fixture.new.members.items():
                self.assertEqual((fixture.install / name).read_bytes(), data, name)
            for name, data in fixture.sentinels.items():
                self.assertEqual((fixture.install / name).read_bytes(), data, name)
            self.assertFalse((fixture.install / "old-only.txt").exists())
            self.assertEqual(apply.read_journal(fixture.plan)[-2]["path"], "main.py")
            self.assertEqual((fixture.transaction / "config-snapshots/before").read_bytes(), fixture.config.read_bytes())
            self.assertEqual(apply.read_json(fixture.transaction.parent / "previous.json"), apply.identity(fixture.plan))
            self.assertTrue(handles.closed)
            launch.assert_called_once()

    def test_parent_exit_without_proceed_never_moves_or_relaunches(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            handles = Handles()
            with mock.patch.object(apply, "relaunch") as launch:
                self.assertEqual(apply.run_helper(fixture.path, parent_pid=os.getpid(), handles=handles), 0)
            fixture.unchanged(self)
            launch.assert_not_called()
            self.assertTrue(handles.closed)
            self.assertFalse(any(r["event"] == "intent" for r in apply.read_journal(fixture.plan)))

    def test_owned_transaction_identity_and_empty_journal_abandon(self):
        with tempfile.TemporaryDirectory() as temp:
            install = Path(temp) / "app space שלום"
            install.mkdir()
            transaction = apply.create_transaction(install)
            self.assertEqual(transaction.parent.name,
                             ".neuralscreen-update-" + apply.install_id_for(install))
            self.assertEqual(json.loads((transaction.parent / "owner.json").read_text(encoding="utf-8"))["install_dir"], str(install.resolve()))
            self.assertFalse((transaction / "journal.jsonl").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
