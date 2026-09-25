"""Deploying the JSON library to a Quest (or any Android device) with adb.

The PianoVision app keeps its own data in the same folder (for example
``finger_position_recordings.json``), so deletion is conservative: only files
this tool has deployed before, and that are no longer part of the library,
are ever removed, and only when asked (``--prune``).
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .library import Library


class DeviceError(RuntimeError):
    pass


class Adb:
    def __init__(self, adb: str = "adb", serial: str = ""):
        self.adb = adb
        self.serial = serial

    def _cmd(self, *args: str) -> List[str]:
        return [self.adb] + (["-s", self.serial] if self.serial else []) + list(args)

    def run(self, *args: str, timeout: float = 300) -> str:
        try:
            p = subprocess.run(self._cmd(*args), capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise DeviceError(f"adb not found ({self.adb!r}); install Android platform-tools")
        except subprocess.TimeoutExpired:
            raise DeviceError(f"adb {' '.join(args[:2])} timed out")
        if p.returncode != 0:
            raise DeviceError((p.stderr or p.stdout).strip() or f"adb {' '.join(args[:2])} failed")
        return p.stdout

    def devices(self) -> List[Tuple[str, str, str]]:
        out = subprocess.run([self.adb, "devices", "-l"], capture_output=True, text=True, timeout=30).stdout
        devs = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                devs.append((parts[0], parts[1], " ".join(parts[2:])))
        return devs

    def connect(self) -> str:
        """Choose the device (the configured serial, or the only one attached)."""
        try:
            devs = self.devices()
        except FileNotFoundError:
            raise DeviceError(f"adb not found ({self.adb!r}); install Android platform-tools")
        ready = [d for d in devs if d[1] == "device"]
        if self.serial:
            match = [d for d in devs if d[0] == self.serial]
            if not match:
                raise DeviceError(f"device {self.serial} is not connected")
            if match[0][1] != "device":
                raise DeviceError(f"device {self.serial} is {match[0][1]} (put on the headset and allow USB debugging)")
            return self.serial
        if not ready:
            waiting = [d for d in devs if d[1] == "unauthorized"]
            if waiting:
                raise DeviceError("device is unauthorized: put on the headset and allow USB debugging")
            raise DeviceError("no device connected (USB cable or `adb connect <ip>`)")
        if len(ready) > 1:
            quests = [d for d in ready if "Quest" in d[2]]
            if len(quests) != 1:
                raise DeviceError("several devices attached; set [device] serial in pianovision.toml or use --serial: "
                                  + ", ".join(d[0] for d in ready))
            ready = quests
        self.serial = ready[0][0]
        return self.serial

    def model(self) -> str:
        for s, _state, desc in self.devices():
            if s == self.serial:
                for kv in desc.split():
                    if kv.startswith("model:"):
                        return kv[6:].replace("_", " ")
        return self.serial

    def md5s(self, directory: str) -> Dict[str, str]:
        d = shlex.quote(directory)
        out = self.run("shell", f"cd {d} 2>/dev/null && for f in *.json; do [ -f \"$f\" ] && md5sum \"$f\"; done; true")
        res = {}
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and len(parts[0]) == 32:
                res[parts[1]] = parts[0]
        return res

    def mkdir(self, directory: str) -> None:
        self.run("shell", f"mkdir -p {shlex.quote(directory)}")

    def push(self, paths: List[str], directory: str, batch: int = 40) -> None:
        for i in range(0, len(paths), batch):
            self.run("push", *paths[i:i + batch], directory.rstrip("/") + "/", timeout=1800)

    def remove(self, directory: str, names: List[str], batch: int = 40) -> None:
        for i in range(0, len(names), batch):
            files = " ".join(shlex.quote(directory.rstrip("/") + "/" + n) for n in names[i:i + batch])
            self.run("shell", f"rm -f -- {files}")


def md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class DeployPlan:
    serial: str
    model: str
    push: List[str] = field(default_factory=list)          # new or changed on the device
    unchanged: List[str] = field(default_factory=list)
    remove: List[str] = field(default_factory=list)        # deployed by us earlier, no longer in the library
    foreign: List[str] = field(default_factory=list)       # JSON files we never deployed (left alone)
    local: Dict[str, str] = field(default_factory=dict)    # name -> md5 of what should be on the device
    device: Dict[str, str] = field(default_factory=dict)
    record: Dict[str, str] = field(default_factory=dict)


def plan_deploy(lib: Library, adb: Adb, force: bool = False) -> DeployPlan:
    """What to push/remove.  ``force`` pushes every library file, even ones already identical."""
    serial = adb.connect()
    plan = DeployPlan(serial=serial, model=adb.model())
    for name in sorted(lib.manifest.outputs()):
        path = lib.out_path(name)
        if not os.path.exists(path):
            continue
        plan.local[name] = md5_file(path)
    plan.device = adb.md5s(lib.cfg.device_dir)
    record = lib.manifest.deployed.get(serial)
    if record is None:
        # First contact: claim the device files that carry a name this library uses or used.
        ours = set(plan.local) | set(lib.output_files()) | {r["output"] for r in lib.manifest.retired}
        record = {n: h for n, h in plan.device.items() if n in ours}
    plan.record = dict(record)
    for name, h in plan.local.items():
        (plan.unchanged if plan.device.get(name) == h and not force else plan.push).append(name)
    for name in sorted(plan.device):
        if name in plan.local:
            continue
        (plan.remove if name in record else plan.foreign).append(name)
    return plan


def execute_deploy(lib: Library, adb: Adb, plan: DeployPlan, prune: bool = False) -> Dict[str, str]:
    """Push (and with ``prune`` remove) as planned; returns the verified device state."""
    d = lib.cfg.device_dir
    if plan.push:
        adb.mkdir(d)
        adb.push([lib.out_path(n) for n in plan.push], d)
    if prune and plan.remove:
        adb.remove(d, plan.remove)
    after = adb.md5s(d)
    bad = [n for n in plan.local if after.get(n) != plan.local[n]]
    record = {n: h for n, h in plan.record.items() if n in after}
    record.update({n: after[n] for n in plan.local if n in after})
    lib.manifest.deployed[plan.serial] = record
    lib.manifest.save()
    if bad:
        raise DeviceError(f"{len(bad)} file(s) did not arrive intact: " + ", ".join(bad[:5]))
    return after
