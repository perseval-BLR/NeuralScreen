"""Standalone, standard-library portable update transaction and recovery helper."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict
from pathlib import Path

try:
    from .update_package import (ManagedFile, UpdateError, _ensure_no_reparse,
                                 _sha256_file, _strict_json, _valid_record,
                                 _protected, _read_installed_inventory,
                                 _check_install)
except ImportError:
    from update_package import (ManagedFile, UpdateError, _ensure_no_reparse,
                                _sha256_file, _strict_json, _valid_record,
                                _protected, _read_installed_inventory,
                                _check_install)

MUTEX_NAME = "NeuralScreen_SingleInstance"
MARKER = ".neuralscreen-update.json"
TRANSPORT = "NEURALSCREEN_UPDATE_PARENT_PATH"
MESSAGE_CODES = {"shutdown_timeout", "apply_failed", "relaunch_failed", "recovery_required"}
PTH = b"python313.zip\n.\n..\n"


def install_id_for(install_dir):
    install_id = hashlib.sha256(
        os.path.normcase(str(Path(install_dir).resolve())).encode("utf-8")
    ).hexdigest()
    return install_id


def control_dir_for(install_dir):
    root = Path(install_dir).resolve()
    return root.parent / (".neuralscreen-update-" + install_id_for(root))


def atomic_json(path, value):
    path = Path(path)
    _ensure_no_reparse(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as output:
            output.write((json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    path = Path(path)
    _ensure_no_reparse(path)
    if path.stat().st_size > 8 * 1024 * 1024:
        raise UpdateError("recovery_required")
    return _strict_json(path.read_bytes())


def identity(plan):
    return {"schema_version": 1, "install_id": plan["install_id"],
            "transaction_id": plan["transaction_id"], "plan_path": str(Path(plan["bootstrap_dir"]).parent / "plan.json")}


def create_transaction(install_dir):
    root = Path(install_dir).resolve()
    _ensure_no_reparse(root)
    control = control_dir_for(root)
    _ensure_no_reparse(control)
    owner = {"schema_version": 1, "install_id": install_id_for(root), "install_dir": str(root)}
    if control.exists():
        if read_json(control / "owner.json") != owner:
            raise UpdateError("recovery_required")
    else:
        control.mkdir()
        atomic_json(control / "owner.json", owner)
    transaction = control / ("txn-" + uuid.uuid4().hex)
    transaction.mkdir()
    return transaction


def _inventory(value):
    if not isinstance(value, list) or not value:
        raise UpdateError("recovery_required")
    result = []
    seen = set()
    for item in value:
        if (not _valid_record(item) or set(item) != {"path", "size", "sha256"}
                or item["path"].casefold() in seen
                or _protected(item["path"]) and item["path"] != "native/libraries/README.md"):
            raise UpdateError("recovery_required")
        seen.add(item["path"].casefold())
        result.append(ManagedFile(**item))
    return tuple(result)


def validate_marker(install_dir):
    root = Path(install_dir).resolve()
    marker = read_json(root / MARKER)
    transaction_id = marker.get("transaction_id")
    if (set(marker) != {"schema_version", "install_id", "transaction_id", "plan_path"}
            or marker.get("schema_version") != 1
            or marker.get("install_id") != install_id_for(root)
            or not isinstance(transaction_id, str) or re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None):
        raise UpdateError("recovery_required")
    expected = control_dir_for(root) / ("txn-" + transaction_id) / "plan.json"
    if marker.get("plan_path") != str(expected):
        raise UpdateError("recovery_required")
    _ensure_no_reparse(expected)
    return expected


def load_plan(path):
    path = Path(path)
    _ensure_no_reparse(path)
    plan = read_json(path)
    try:
        root = Path(plan["install_dir"])
        transaction = path.parent
        tid = plan["transaction_id"]
        if (plan["schema_version"] != 1 or not root.is_absolute() or str(root.resolve()) != str(root)
                or plan["install_id"] != install_id_for(root)
                or re.fullmatch(r"[0-9a-f]{32}", tid) is None
                or transaction != control_dir_for(root) / ("txn-" + tid)
                or path != transaction / "plan.json"):
            raise ValueError()
        for key, name in (("candidate_dir", "candidate"), ("rollback_dir", "rollback"), ("bootstrap_dir", "bootstrap")):
            if plan[key] != str(transaction / name):
                raise ValueError()
            _ensure_no_reparse(Path(plan[key]))
        _ensure_no_reparse(root)
        owner = {"schema_version": 1, "install_id": plan["install_id"], "install_dir": str(root)}
        if read_json(transaction.parent / "owner.json") != owner:
            raise ValueError()
        if (isinstance(plan["parent_pid"], bool) or not isinstance(plan["parent_pid"], int)
                or plan["parent_pid"] <= 0):
            raise ValueError()
        for key in ("effective_config_path", "original_parent_cwd"):
            value = Path(plan[key])
            if not value.is_absolute() or str(value.resolve()) != str(value):
                raise ValueError()
            _ensure_no_reparse(value)
        config = Path(plan["effective_config_path"])
        for forbidden in (transaction.parent,):
            if config == forbidden or forbidden in config.parents:
                raise ValueError()
        messages = plan["messages"]
        if (not isinstance(messages, dict) or set(messages) != MESSAGE_CODES
                or any(not isinstance(v, str) or not v.strip() or len(v) > 16000 or "\0" in v for v in messages.values())):
            raise ValueError()
        old, new = _inventory(plan["old_inventory"]), _inventory(plan["new_inventory"])
        if root in config.parents and config.relative_to(root).as_posix().casefold() in {r.path.casefold() for r in (*old, *new)}:
            raise ValueError()
        names = sorted({r.path for r in (*old, *new)}, key=lambda n: (n == "main.py", n))
        if plan["operations"] != names:
            raise ValueError()
        if not isinstance(plan["bootstrap_inventory"], list) or not plan["bootstrap_inventory"]:
            raise ValueError()
        for item in plan["bootstrap_inventory"]:
            if not _valid_record(item):
                raise ValueError()
        return plan
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise UpdateError("recovery_required") from exc


def verify_records(root, records):
    for item in records:
        path = Path(root) / item.path
        _ensure_no_reparse(path)
        if not path.is_file() or _sha256_file(path) != (item.size, item.sha256):
            raise UpdateError("modified_install")


def helper_environment(parent_env):
    if TRANSPORT in parent_env:
        raise UpdateError("environment_conflict")
    env = dict(parent_env)
    env[TRANSPORT] = env.get("PATH", "")
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    env["PATH"] = os.pathsep.join((str(system / "System32"), str(system)))
    return env


def relaunch_environment(env):
    result = dict(env)
    if TRANSPORT in result:
        result["PATH"] = result.pop(TRANSPORT)
    result.pop("PYTHONHOME", None)
    result.pop("PYTHONPATH", None)
    return result


def prepare_plan(staged, *, config_path, original_cwd, messages, parent_pid=None):
    if not staged.can_install:
        raise UpdateError(staged.refusal_code or "install_readonly")
    transaction = Path(staged.transaction_dir).resolve()
    owner = read_json(transaction.parent / "owner.json")
    root = Path(owner["install_dir"])
    if transaction.parent != control_dir_for(root) or not re.fullmatch(r"txn-[0-9a-f]{32}", transaction.name):
        raise UpdateError("invalid_package")
    old, version, refusal = _check_install(root, transaction, staged.new_inventory, staged.version, staged.installed_version)
    if refusal or old != staged.old_inventory:
        raise UpdateError(refusal or "modified_install")
    cwd = Path(original_cwd).resolve()
    config = (cwd / config_path).resolve()
    # Only old-inventory runtime core and local helper bytes can execute externally.
    selected = [r for r in old if (r.path.startswith("runtime/") and not r.path.startswith("runtime/Lib/"))
                or r.path in {"app/update_apply.py", "app/update_package.py"}]
    required = {"runtime/python.exe", "runtime/pythonw.exe", "runtime/python313.zip", "runtime/python313.dll",
                "runtime/python313._pth", "runtime/_ctypes.pyd", "app/update_apply.py", "app/update_package.py"}
    if not required.issubset({r.path for r in selected}):
        raise UpdateError("install_readonly")
    bootstrap = transaction / "bootstrap"
    if bootstrap.exists():
        raise UpdateError("invalid_package")
    size = sum(r.size for r in selected) + (config.stat().st_size if config.is_file() else 0)
    if shutil.disk_usage(transaction).free < size + 128 * 1024 * 1024:
        raise UpdateError("disk_space")
    bootstrap.mkdir()
    copied = []
    for record in selected:
        verify_records(root, (record,))
        relative = record.path if record.path.startswith("runtime/") else Path(record.path).name
        target = bootstrap / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / record.path, target)
        if _sha256_file(target) != (record.size, record.sha256):
            raise UpdateError("checksum")
        if relative == "runtime/python313._pth":
            target.write_bytes(PTH)
        copied.append({"path": str(relative).replace("\\", "/"), "size": target.stat().st_size,
                       "sha256": _sha256_file(target)[1]})
    plan = {"schema_version": 1, "install_id": install_id_for(root), "transaction_id": transaction.name[4:],
            "install_dir": str(root), "candidate_dir": str(transaction / "candidate"),
            "rollback_dir": str(transaction / "rollback"), "bootstrap_dir": str(bootstrap),
            "parent_pid": parent_pid or os.getpid(), "effective_config_path": str(config),
            "original_parent_cwd": str(cwd), "messages": messages,
            "old_inventory": [asdict(r) for r in old], "new_inventory": [asdict(r) for r in staged.new_inventory],
            "operations": sorted({r.path for r in (*old, *staged.new_inventory)}, key=lambda n: (n == "main.py", n)),
            "bootstrap_inventory": copied}
    plan_path = transaction / "plan.json"
    if plan_path.exists():
        raise UpdateError("invalid_package")
    atomic_json(plan_path, plan)
    load_plan(plan_path)
    return plan_path


def verify_bootstrap(plan):
    verify_records(Path(plan["bootstrap_dir"]), _inventory(plan["bootstrap_inventory"]))
    if (Path(plan["bootstrap_dir"]) / "runtime/python313._pth").read_bytes() != PTH:
        raise UpdateError("recovery_required")


def protocol(plan, name):
    path = Path(plan["bootstrap_dir"]).parent / (name + ".json")
    if not path.exists():
        return False
    if read_json(path) != identity(plan):
        raise UpdateError("recovery_required")
    return True


def write_protocol(plan, name):
    atomic_json(Path(plan["bootstrap_dir"]).parent / (name + ".json"), identity(plan))


def read_journal(plan):
    path = Path(plan["bootstrap_dir"]).parent / "journal.jsonl"
    _ensure_no_reparse(path)
    if path.stat().st_size > 64 * 1024 * 1024:
        raise UpdateError("recovery_required")
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        raise UpdateError("recovery_required")
    records = []
    allowed = {"intent", "committed", "restored", "abandoned", "config_snapshot", "config_preserved", "config_restored"}
    intents = []
    for line in data.splitlines():
        item = _strict_json(line)
        if (item.get("install_id") != plan["install_id"] or item.get("transaction_id") != plan["transaction_id"]
                or item.get("event") not in allowed):
            raise UpdateError("recovery_required")
        if item["event"] == "intent" and item.get("path") not in plan["operations"]:
            raise UpdateError("recovery_required")
        event = item["event"]
        fields = {"install_id", "transaction_id", "event"}
        if event == "intent":
            fields.add("path")
            intents.append(item["path"])
            if intents != plan["operations"][:len(intents)]:
                raise UpdateError("recovery_required")
        elif event in {"committed", "restored", "config_preserved", "config_restored"} and not intents:
            raise UpdateError("recovery_required")
        if event == "committed" and intents != plan["operations"]:
            raise UpdateError("recovery_required")
        if event == "config_snapshot":
            fields.add("present")
            if not isinstance(item.get("present"), bool):
                raise UpdateError("recovery_required")
        if event == "config_preserved" or event == "config_snapshot" and item["present"]:
            fields.update({"size", "sha256"})
            if (isinstance(item.get("size"), bool) or not isinstance(item.get("size"), int) or item["size"] < 0
                    or not isinstance(item.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None):
                raise UpdateError("recovery_required")
        if set(item) != fields:
            raise UpdateError("recovery_required")
        records.append(item)
    return records


def append_journal(plan, event, **fields):
    path = Path(plan["bootstrap_dir"]).parent / "journal.jsonl"
    _ensure_no_reparse(path)
    value = {"install_id": plan["install_id"], "transaction_id": plan["transaction_id"], "event": event, **fields}
    # r+b cannot accidentally recreate an uncertain missing journal.
    with path.open("r+b") as output:
        output.seek(0, 2)
        output.write((json.dumps(value, sort_keys=True) + "\n").encode())
        output.flush()
        os.fsync(output.fileno())


def clear_uncommitted(plan):
    for name in ("armed", "proceed", "cancel"):
        if protocol(plan, name):
            (Path(plan["bootstrap_dir"]).parent / (name + ".json")).unlink()
    marker = Path(plan["install_dir"]) / MARKER
    if marker.exists():
        if read_json(marker) != identity(plan):
            raise UpdateError("recovery_required")
        marker.unlink()


def abandon_no_moves(plan):
    records = read_journal(plan)
    if any(r["event"] == "intent" for r in records):
        return False
    append_journal(plan, "abandoned")
    clear_uncommitted(plan)
    return True


def _matches(path, record):
    _ensure_no_reparse(path)
    return path.is_file() and _sha256_file(path) == (record.size, record.sha256)


def _replace(source, target):
    deadline = time.monotonic() + 10
    while True:
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (32, 33) or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def copy_durable(source, target):
    _ensure_no_reparse(source)
    _ensure_no_reparse(target)
    temporary = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex)
    _ensure_no_reparse(temporary)
    expected = _sha256_file(source)
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if _sha256_file(temporary) != expected:
            raise UpdateError("recovery_required")
        _replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return expected


def restore_payload(plan, *, manual=False):
    records = read_journal(plan)
    if not any(r["event"] == "intent" for r in records):
        abandon_no_moves(plan)
        return
    root, rollback = Path(plan["install_dir"]), Path(plan["rollback_dir"])
    marker = root / MARKER
    if marker.exists():
        if read_json(marker) != identity(plan):
            raise UpdateError("recovery_required")
    else:
        atomic_json(marker, identity(plan))
    old = {r.path: r for r in _inventory(plan["old_inventory"])}
    new = {r.path: r for r in _inventory(plan["new_inventory"])}
    snapshot = rollback.parent / "config-snapshots" / "before"
    config_record = next((r for r in records if r["event"] == "config_snapshot"), None)
    if manual and config_record and config_record.get("present"):
        if not _matches(snapshot, ManagedFile("before", config_record["size"], config_record["sha256"])):
            raise UpdateError("recovery_required")
    intents = list(dict.fromkeys(r["path"] for r in records if r["event"] == "intent"))
    for name in reversed(intents):
        destination, backup = root / name, rollback / name
        if name in old and _matches(destination, old[name]):
            continue
        if name in old and not _matches(backup, old[name]):
            raise UpdateError("recovery_required")
        if destination.exists():
            if name not in new or not _matches(destination, new[name]):
                raise UpdateError("recovery_required")
            destination.unlink()
        if name in old:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Copy recovery bytes so repeated restore never consumes the last backup.
            temporary = rollback.parent / "restore" / name
            temporary.parent.mkdir(parents=True, exist_ok=True)
            _ensure_no_reparse(temporary)
            if temporary.exists():
                if not _matches(temporary, old[name]):
                    raise UpdateError("recovery_required")
            else:
                copy_durable(backup, temporary)
            _replace(temporary, destination)
    verify_records(root, tuple(old.values()))
    if manual:
        config = Path(plan["effective_config_path"])
        if config_record and config_record.get("present") and not any(r["event"] == "config_restored" for r in records):
            if config.exists():
                preserved = snapshot.with_name("after")
                if not preserved.exists():
                    size, digest = copy_durable(config, preserved)
                    append_journal(plan, "config_preserved", size=size, sha256=digest)
                elif not any(r["event"] == "config_preserved" for r in records):
                    if _sha256_file(preserved) != _sha256_file(config):
                        raise UpdateError("recovery_required")
                    size, digest = _sha256_file(preserved)
                    append_journal(plan, "config_preserved", size=size, sha256=digest)
            config.parent.mkdir(parents=True, exist_ok=True)
            copy_durable(snapshot, config)
            append_journal(plan, "config_restored")
    append_journal(plan, "restored")
    clear_uncommitted(plan)


def apply_payload(plan):
    root, candidate, rollback = (Path(plan[k]) for k in ("install_dir", "candidate_dir", "rollback_dir"))
    old, new = _inventory(plan["old_inventory"]), _inventory(plan["new_inventory"])
    old_actual, old_version = _read_installed_inventory(root)
    if old_actual != old:
        raise UpdateError("modified_install")
    candidate_actual, target_version = _read_installed_inventory(candidate)
    if candidate_actual != new:
        raise UpdateError("checksum")
    checked, _, refusal = _check_install(root, rollback.parent, new, target_version, old_version)
    if refusal or checked != old:
        raise UpdateError(refusal or "modified_install")
    verify_bootstrap(plan)
    if read_journal(plan):
        raise UpdateError("recovery_required")
    if protocol(plan, "cancel"):
        abandon_no_moves(plan)
        return False
    snapshot_dir = rollback.parent / "config-snapshots"
    snapshot_dir.mkdir()
    config = Path(plan["effective_config_path"])
    if config.is_file():
        _ensure_no_reparse(config)
        size, digest = copy_durable(config, snapshot_dir / "before")
        append_journal(plan, "config_snapshot", present=True, size=size, sha256=digest)
    else:
        append_journal(plan, "config_snapshot", present=False)
    old_by = {r.path: r for r in old}
    new_by = {r.path: r for r in new}
    started = False
    for name in plan["operations"]:
        if not started and protocol(plan, "cancel"):
            abandon_no_moves(plan)
            return False
        if name == "main.py":
            verify_records(root, tuple(r for r in new if r.path != "main.py"))
        append_journal(plan, "intent", path=name)
        if not started and protocol(plan, "cancel"):
            restore_payload(plan)
            return False
        started = True
        destination, backup = root / name, rollback / name
        _ensure_no_reparse(destination)
        _ensure_no_reparse(backup)
        if name in old_by:
            if not _matches(destination, old_by[name]):
                raise UpdateError("modified_install")
            backup.parent.mkdir(parents=True, exist_ok=True)
            _replace(destination, backup)
        elif destination.exists():
            raise UpdateError("modified_install")
        if name in new_by:
            destination.parent.mkdir(parents=True, exist_ok=True)
            _replace(candidate / name, destination)
    verify_records(root, new)
    verify_records(rollback, old)
    append_journal(plan, "committed")
    # Publishing only after a complete verified rollback inventory keeps the prior backup safe.
    previous_path = rollback.parent.parent / "previous.json"
    previous = read_json(previous_path) if previous_path.exists() else None
    atomic_json(previous_path, identity(plan))
    clear_uncommitted(plan)
    if previous is not None:
        cleanup_previous(previous, plan)
    return True


def cleanup_previous(previous, current):
    """Remove only a complete older owned backup after its successor is durable."""
    try:
        path = Path(previous["plan_path"])
        old = load_plan(path)
        if (previous != identity(old) or old["install_id"] != current["install_id"]
                or old["transaction_id"] == current["transaction_id"]):
            return
        if not any(r["event"] == "committed" for r in read_journal(old)):
            return
        verify_records(Path(old["rollback_dir"]), _inventory(old["old_inventory"]))
        verify_bootstrap(old)
        allowed = {"plan.json", "journal.jsonl", "release.zip", "updater.log"}
        allowed.update("rollback/" + r.path for r in _inventory(old["old_inventory"]))
        allowed.update("bootstrap/" + r["path"] for r in old["bootstrap_inventory"])
        allowed.update({"config-snapshots/before", "config-snapshots/after"})
        directories = {"candidate", "rollback", "bootstrap", "config-snapshots", "restore"}
        for record in _inventory(old["old_inventory"]):
            directories.update("restore/" + str(p).replace("\\", "/") for p in Path(record.path).parents if str(p) != ".")
        for record in _inventory(old["new_inventory"]):
            directories.update("candidate/" + str(p).replace("\\", "/") for p in Path(record.path).parents if str(p) != ".")
        for name in allowed:
            directories.update(str(p).replace("\\", "/") for p in Path(name).parents if str(p) != ".")
        for folder, dirs, files in os.walk(path.parent, followlinks=False):
            for name in dirs + files:
                entry = Path(folder) / name
                _ensure_no_reparse(entry)
                relative = entry.relative_to(path.parent).as_posix()
                if relative not in (directories if entry.is_dir() else allowed):
                    return
        # Verified absolute txn path, never the control root or current bootstrap.
        shutil.rmtree(path.parent)
    except (OSError, ValueError, KeyError, UpdateError):
        # Unexpected evidence retains the older copy. Cleanup never invalidates commit.
        return


def discard_download(transaction_dir, install_dir):
    """Discard a failed, internally allocated download before any helper preparation."""
    try:
        root, transaction = Path(install_dir), Path(transaction_dir)
        _ensure_no_reparse(root)
        _ensure_no_reparse(transaction)
        root = root.resolve()
        control = control_dir_for(root)
        if (transaction.parent != control or re.fullmatch(r"txn-[0-9a-f]{32}", transaction.name) is None
                or read_json(control / "owner.json") != {
                    "schema_version": 1, "install_id": install_id_for(root), "install_dir": str(root)}):
            raise UpdateError("recovery_required")
        marker = root / MARKER
        if marker.exists() or marker.is_symlink():
            raise UpdateError("recovery_required")
        previous = control / "previous.json"
        _ensure_no_reparse(previous)
        if previous.exists():
            value = read_json(previous)
            if (not isinstance(value, dict) or not isinstance(value.get("plan_path"), str)
                    or "\0" in value["plan_path"] or not Path(value["plan_path"]).is_absolute()
                    or Path(value["plan_path"]).resolve().is_relative_to(transaction)):
                raise UpdateError("recovery_required")
        if not transaction.exists():
            return
        if not transaction.is_dir():
            raise UpdateError("recovery_required")
        pending = [transaction]
        while pending:
            for entry in pending.pop().iterdir():
                _ensure_no_reparse(entry)
                directory = entry.is_dir()
                if entry.parent == transaction and (
                        entry.name not in {"release.zip", "release.zip.part", "candidate"}
                        or directory != (entry.name == "candidate")):
                    raise UpdateError("recovery_required")
                if directory:
                    pending.append(entry)
                elif not entry.is_file():
                    raise UpdateError("recovery_required")
        shutil.rmtree(transaction)
    except ValueError as exc:
        raise UpdateError("recovery_required") from exc
    except OSError as exc:
        raise UpdateError("prepare_failed") from exc


def discard_staged(staged, install_dir):
    """Discard an owned candidate only before arming or after settled abandonment."""
    try:
        root, transaction = Path(install_dir), Path(staged.transaction_dir)
        _ensure_no_reparse(root)
        _ensure_no_reparse(transaction)
        root = root.resolve()
        control = control_dir_for(root)
        if (transaction.parent != control or re.fullmatch(r"txn-[0-9a-f]{32}", transaction.name) is None
                or Path(staged.candidate_dir) != transaction / "candidate"
                or Path(staged.archive_path) != transaction / "release.zip"
                or read_json(control / "owner.json") != {
                    "schema_version": 1, "install_id": install_id_for(root), "install_dir": str(root)}):
            raise UpdateError("recovery_required")
        marker = root / MARKER
        if marker.exists() or marker.is_symlink():
            raise UpdateError("recovery_required")
        previous = control / "previous.json"
        _ensure_no_reparse(previous)
        if previous.exists():
            value = read_json(previous)
            if (not isinstance(value, dict) or not isinstance(value.get("plan_path"), str)
                    or Path(value["plan_path"]).resolve() == transaction / "plan.json"):
                raise UpdateError("recovery_required")
        if not transaction.exists():
            return
        if not transaction.is_dir():
            raise UpdateError("recovery_required")
        allowed = {"release.zip", "plan.json"}
        allowed.update("candidate/" + r.path for r in staged.new_inventory)
        for record in staged.old_inventory:
            if record.path.startswith("runtime/") and not record.path.startswith("runtime/Lib/"):
                allowed.add("bootstrap/" + record.path)
            elif record.path in {"app/update_apply.py", "app/update_package.py"}:
                allowed.add("bootstrap/" + Path(record.path).name)
        journal = transaction / "journal.jsonl"
        _ensure_no_reparse(journal)
        if journal.exists():
            plan = load_plan(transaction / "plan.json")
            records = read_journal(plan)
            if any(r["event"] == "intent" for r in records) or not any(r["event"] == "abandoned" for r in records):
                raise UpdateError("recovery_required")
            allowed.update({"journal.jsonl", "updater.log"})
            for name in ("armed", "proceed", "cancel", "cancelled"):
                if protocol(plan, name):
                    allowed.add(name + ".json")
        directories = {"candidate", "bootstrap"}
        for name in allowed:
            directories.update(p.as_posix() for p in Path(name).parents if str(p) != ".")
        for folder, dirs, files in os.walk(transaction, followlinks=False):
            for name in dirs + files:
                entry = Path(folder) / name
                _ensure_no_reparse(entry)
                if entry.relative_to(transaction).as_posix() not in (directories if entry.is_dir() else allowed):
                    raise UpdateError("recovery_required")
        shutil.rmtree(transaction)
    except OSError as exc:
        raise UpdateError("prepare_failed") from exc


class WindowsHandles:
    """Own exclusion on the helper's calling thread and pin the exact parent."""
    def __init__(self):
        from ctypes import wintypes as wt
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateMutexW", [wt.LPVOID, wt.BOOL, wt.LPCWSTR], wt.HANDLE),
            ("OpenProcess", [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
            ("WaitForSingleObject", [wt.HANDLE, wt.DWORD], wt.DWORD),
            ("ReleaseMutex", [wt.HANDLE], wt.BOOL), ("CloseHandle", [wt.HANDLE], wt.BOOL)):
            fn = getattr(self.api, name)
            fn.argtypes, fn.restype = args, result
        self.mutex = self.parent = None
        self.owned = False

    def acquire(self, parent_pid=None):
        ctypes.set_last_error(0)
        self.mutex = self.api.CreateMutexW(None, False, MUTEX_NAME)
        error = ctypes.get_last_error()
        if not self.mutex or error not in (0, 183):
            raise UpdateError("recovery_required")
        # A manual restore requires a fresh object, hence no live parent/helper.
        if parent_pid is None and error == 183:
            raise UpdateError("recovery_required")
        result = self.api.WaitForSingleObject(self.mutex, 0)
        self.owned = result in (0, 0x80)
        if result != 0:
            raise UpdateError("recovery_required")
        if parent_pid is not None:
            self.parent = self.api.OpenProcess(0x00100000, False, parent_pid)
            if not self.parent:
                raise UpdateError("recovery_required")

    def exited(self):
        result = self.api.WaitForSingleObject(self.parent, 0)
        if result not in (0, 258):
            raise UpdateError("recovery_required")
        return result == 0

    def close(self):
        if self.parent:
            self.api.CloseHandle(self.parent)
            self.parent = None
        if self.mutex:
            if self.owned:
                self.api.ReleaseMutex(self.mutex)
                self.owned = False
            self.api.CloseHandle(self.mutex)
            self.mutex = None


def restore_command(plan_path):
    path = Path(plan_path)
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    return "& " + quote(path.parent / "bootstrap/runtime/python.exe") + " -I -S " + quote(path.parent / "bootstrap/update_apply.py") + " --restore " + quote(path)


def show_notice(text):
    from ctypes import wintypes as wt
    api = ctypes.WinDLL("user32", use_last_error=True)
    api.MessageBoxW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT]
    api.MessageBoxW.restype = ctypes.c_int
    return bool(api.MessageBoxW(None, text, "NeuralScreen", 0x00010000))


def relaunch(plan, env):
    root = Path(plan["install_dir"])
    subprocess.Popen([str(root / "runtime/pythonw.exe"), "-u", str(root / "main.py"),
                      "--config", plan["effective_config_path"]], cwd=plan["original_parent_cwd"],
                     env=relaunch_environment(env), close_fds=True, shell=False)


def run_helper(plan_path, *, parent_pid=None, restore=False, handles=None, env=None,
               arm_timeout=10.0, shutdown_timeout=60.0):
    try:
        plan = load_plan(plan_path)
        verify_bootstrap(plan)
        if not restore and (parent_pid != plan["parent_pid"] or (Path(plan_path).parent / "journal.jsonl").exists()):
            raise UpdateError("recovery_required")
    except Exception:
        text = ("NeuralScreen cannot safely start after an interrupted update.\n"
                "Keep the recovery files.\n"
                "Official releases: https://github.com/perseval-BLR/NeuralScreen/releases")
        try:
            path = Path(plan_path)
            owner = read_json(path.parent.parent / "owner.json")
            root = Path(owner["install_dir"])
            if (owner != {"schema_version": 1, "install_id": install_id_for(root), "install_dir": str(root.resolve())}
                    or path != validate_marker(root)):
                raise UpdateError("recovery_required")
            text += "\nRecovery folder: " + str(control_dir_for(root)) + "\nClose all NeuralScreen and updater windows before manual recovery.\n" + restore_command(path)
        except Exception:
            text += "\nThe recovery command could not be determined. Keep this folder and use the official release page for manual recovery."
        try:
            show_notice(text)
        except Exception:
            pass
        return 1
    handles = handles or WindowsHandles()
    notice = None
    restart = False
    status = 0
    initialized = False
    acquired = False
    authorized = False
    try:
        handles.acquire(None if restore else parent_pid)
        acquired = True
        if restore:
            restore_payload(plan, manual=True)
        else:
            journal = Path(plan_path).parent / "journal.jsonl"
            with journal.open("xb") as output:
                output.flush()
                os.fsync(output.fileno())
            initialized = True
            write_protocol(plan, "armed")
            deadline = time.monotonic() + arm_timeout
            while True:
                if protocol(plan, "cancel"):
                    abandon_no_moves(plan)
                    write_protocol(plan, "cancelled")
                    break
                if not authorized and protocol(plan, "proceed"):
                    if validate_marker(plan["install_dir"]) != Path(plan_path):
                        raise UpdateError("recovery_required")
                    authorized = True
                    deadline = time.monotonic() + shutdown_timeout
                exited = handles.exited()
                if authorized and exited:
                    restart = apply_payload(plan)
                    break
                if (exited and not authorized) or time.monotonic() >= deadline:
                    abandon_no_moves(plan)
                    if authorized:
                        notice, status = "shutdown_timeout", 1
                    break
                time.sleep(0.025)
    except Exception:
        status = 1
        try:
            if restore or not acquired or not initialized:
                raise UpdateError("recovery_required")
            records = read_journal(plan)
            if any(r["event"] == "intent" for r in records):
                restore_payload(plan)
                notice, restart = "apply_failed", True
            else:
                abandon_no_moves(plan)
                if authorized and handles.exited():
                    verify_records(Path(plan["install_dir"]), _inventory(plan["old_inventory"]))
                    notice, restart = "apply_failed", True
                else:
                    notice = "recovery_required"
        except Exception:
            notice, restart = "recovery_required", False
    finally:
        handles.close()
    if restart:
        try:
            relaunch(plan, os.environ if env is None else env)
        except (OSError, ValueError):
            notice, status = "relaunch_failed", 1
    if notice:
        # Fixed codes only. Never log exception strings, paths from environment, or credentials.
        log = Path(plan_path).parent / "updater.log"
        try:
            _ensure_no_reparse(log)
            if not log.exists() or log.stat().st_size < 65536:
                with log.open("a", encoding="utf-8") as output:
                    output.write(notice + "\n")
        except (OSError, UpdateError):
            pass
        text = plan["messages"][notice]
        if notice in {"recovery_required", "relaunch_failed", "apply_failed"}:
            text += "\n" + restore_command(plan_path)
        try:
            shown = show_notice(text)
        except Exception:
            shown = False
        if not shown:
            status = 1
    return status


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", type=Path)
    mode.add_argument("--restore", type=Path)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    try:
        return run_helper(args.restore or args.plan, parent_pid=args.parent_pid, restore=args.restore is not None)
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
