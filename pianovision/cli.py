"""Command line: ``python -m pianovision <command>``.

    status    what would change; library health (invalid files, collisions, orphans)
    build     render new/edited scores, retire removed ones (incremental)
    deploy    copy the library to the Quest over adb (changed files only)
    sync      build, then deploy
    watch     keep building (and deploying) as scores are saved
    verify    re-render everything and compare with the pinned outputs
    rename    re-name outputs after score titles were edited
    convert   one .mscz -> .json (no library, no manifest)
    hands     hand edits recorded on the headset -> the scores (pull | review | apply)
    gui       the Piano Library window (library, build, push to the headset, hand edits)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional

from .library import Library, load_config, scan_library


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _list(title: str, items: List[str], limit: int = 12) -> None:
    if not items:
        return
    _p(f"{title} ({len(items)}):")
    for it in items[:limit]:
        _p(f"  {it}")
    if len(items) > limit:
        _p(f"  ... and {len(items) - limit} more")


def _library(args) -> Library:
    cfg = load_config(args.config, scores=args.scores, output=args.output)
    if getattr(args, "serial", None):
        cfg.serial = args.serial
    if not cfg.scores:
        sys.exit("no scores folder: set [library] scores in pianovision.toml or pass --scores")
    return Library(cfg)


# ------------------------------------------------------------------------------

def cmd_status(args) -> int:
    lib = _library(args)
    st = lib.status()
    scan, c = st["scan"], st["counts"]
    _p(f"scores:  {lib.cfg.scores}")
    _p(f"output:  {lib.cfg.output}")
    _p(f"{len(scan.sources)} scores; {c.get('keep', 0)} up to date, {c.get('render', 0)} to render, "
       f"{c.get('parts', 0)} Note Waterfall parts files to write, "
       f"{c.get('move', 0)} moved, {c.get('retire', 0)} to retire, {c.get('conflict', 0)} conflicts")
    _print_parts_summary(st["parts"])
    _list("to render", [f"{a.rel}  ({a.reason})" for a in st["actions"] if a.kind == "render"])
    _list("moved", [f"{a.old_rel} -> {a.rel}" for a in st["actions"] if a.kind == "move"])
    _list("to retire", [f"{a.rel}  ({a.reason})" for a in st["actions"] if a.kind == "retire"])
    _list("outputs edited outside the pipeline (build --force to replace)",
          [a.rel for a in st["actions"] if a.kind == "conflict"])
    _list("not usable as scores", [f"{r}: {why}" for r, why in sorted(scan.invalid.items())])
    _list("orphan outputs (no score; `build --prune-orphans` retires them)", st["orphans"])
    _list("title changed since the output was named (`rename` to follow)",
          [f"{o} -> {n}" for _r, o, n in st["drift"]])
    _list("pinned outputs the renderer does not reproduce exactly (kept as is)", st["pinned"])
    _p(f"excluded (.zip): {len(scan.excluded)}; companion .mid files (no longer needed): "
       f"{len(scan.companion_midi)}")
    _list("stray .mid files without a score", scan.stray_midi, 5)
    if args.device:
        return _device_status(lib, args)
    return 0


def _device_status(lib: Library, args) -> int:
    from .device import Adb, DeviceError, plan_deploy
    try:
        plan = plan_deploy(lib, Adb(lib.cfg.adb, lib.cfg.serial))
    except DeviceError as e:
        _p(f"device: {e}")
        return 1
    _p(f"device: {plan.model} ({plan.serial}): {len(plan.unchanged)} current, {len(plan.push)} to push, "
       f"{len(plan.remove)} removable, {len(plan.foreign)} other JSON files left alone")
    return 0


def _print_parts_summary(s: dict) -> None:
    _p(f"Note Waterfall parts (NoteWaterfall/*.parts.json): {s['parts']} of {s['scores']} scores; "
       f"{s['simplified']} with a simplified part, {s['orchestra']} with orchestra notes "
       f"({s['piano_orchestral']} piano-orchestral staves, {s['instruments']} orchestra instruments; "
       f"{s['orchestra_notes']} notes)")


def _print_build(rep, dry: bool, lib: Optional[Library] = None) -> None:
    if dry:
        kinds = {}
        for a in rep.planned:
            kinds.setdefault(a.kind, []).append(a)
        _p(f"dry run: {len(kinds.get('keep', []))} up to date")
        _list("would render", [f"{a.rel}  ({a.reason})" for a in kinds.get("render", [])])
        _list("would write Note Waterfall parts", [f"{a.rel}  ({a.reason})" for a in kinds.get("parts", [])])
        _list("would move", [f"{a.old_rel} -> {a.rel}" for a in kinds.get("move", [])])
        _list("would retire", [f"{a.rel}  ({a.reason})" for a in kinds.get("retire", [])])
        _list("conflicts", [f"{r}: {w}" for r, w in rep.conflicts])
        return
    _list("adopted existing outputs", [f"{o}  <- {r}  [{how}]" for r, o, how in rep.adopted], 8)
    _list("written", [f"{o}  <- {r}  ({why})" for r, o, why in rep.written])
    _list("moved", [f"{a} -> {b}" for a, b in rep.moved])
    _list("retired", [f"{o}  ({why})" for _r, o, why in rep.retired])
    _list("conflicts (not overwritten; use --force)", [f"{r}: {w}" for r, w in rep.conflicts])
    _list("FAILED", [f"{r}: {e}" for r, e in rep.failed], 50)
    _list("moved aside to the attic", rep.attic)
    _list("Note Waterfall parts written", rep.parts, 8)
    _p(f"{rep.kept} up to date, {len(rep.written)} written, {len(rep.adopted)} adopted, "
       f"{len(rep.retired)} retired, {len(rep.parts)} parts files written, {len(rep.failed)} failed")
    if lib is not None:
        _print_parts_summary(lib.parts_summary())


def _write_report(path: Optional[str], lib: Library, rep) -> None:
    """``--report FILE``: what the build did, as JSON (read by the GUI to show failed scores)."""
    if not path:
        return
    import json
    failed = []
    for rel, err in rep.failed:
        try:
            st = os.stat(os.path.join(lib.cfg.scores, rel))
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            mtime = size = None
        failed.append({"rel": rel, "error": err, "mtime": mtime, "size": size})
    doc = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "failed": failed,
           "written": [{"rel": r, "output": o, "why": w} for r, o, w in rep.written],
           "retired": [{"rel": r, "output": o, "why": w} for r, o, w in rep.retired],
           "parts": list(rep.parts)}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def cmd_build(args) -> int:
    lib = _library(args)
    rep = lib.build(force=args.force is not None, only=args.force or None, dry_run=args.dry_run,
                    prune_orphans=args.prune_orphans, jobs=args.jobs)
    _print_build(rep, args.dry_run, lib)
    if not args.dry_run:
        _write_report(args.report, lib, rep)
    return 1 if rep.failed else 0


def _deploy(lib: Library, args) -> int:
    from .device import Adb, DeviceError, execute_deploy, plan_deploy
    adb = Adb(lib.cfg.adb, lib.cfg.serial)
    try:
        plan = plan_deploy(lib, adb, force=args.force_push)
    except DeviceError as e:
        _p(f"deploy: {e}")
        return 2
    _p(f"device: {plan.model} ({plan.serial})  {lib.cfg.device_dir}")
    _p(f"  {len(plan.push)} to push, {len(plan.unchanged)} already current, "
       f"{len(plan.remove)} no longer in the library, {len(plan.foreign)} other JSON files (left alone)")
    _list("  push", plan.push, 8)
    _list("  not in the library any more" + ("" if args.prune else " (kept; --prune removes)"), plan.remove)
    _list("  not ours, never touched", plan.foreign, 5)
    if args.dry_run:
        return 0
    prune = args.prune and bool(plan.remove)
    if prune and not args.yes:
        if not sys.stdin.isatty():
            _p("  refusing to delete from the device without --yes")
            prune = False
        else:
            ans = input(f"  delete {len(plan.remove)} file(s) from the device? [y/N] ").strip().lower()
            prune = ans in ("y", "yes")
    if not plan.push and not prune:
        _p("  nothing to do")
        return 0
    t = time.monotonic()
    try:
        execute_deploy(lib, adb, plan, prune=prune)
    except DeviceError as e:
        _p(f"deploy failed: {e}")
        return 2
    _p(f"  pushed {len(plan.push)}" + (f", removed {len(plan.remove)}" if prune else "")
       + f"; verified on device ({time.monotonic() - t:.0f}s)")
    return 0


def cmd_deploy(args) -> int:
    return _deploy(_library(args), args)


def cmd_sync(args) -> int:
    lib = _library(args)
    rep = lib.build(jobs=args.jobs)
    _print_build(rep, False, lib)
    _write_report(args.report, lib, rep)
    rc = _deploy(lib, args)
    return rc or (1 if rep.failed else 0)


def cmd_watch(args) -> int:
    from .device import Adb, DeviceError, execute_deploy, plan_deploy
    lib = _library(args)
    root = lib.cfg.scores

    def snapshot():
        snap = {}
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if not d.startswith(".")]
            for f in fn:
                if f.lower().endswith((".mscz", ".zip")) or "." not in f:
                    try:
                        st = os.stat(os.path.join(dp, f))
                    except OSError:
                        continue
                    snap[os.path.join(dp, f)] = (st.st_mtime, st.st_size)
        return snap

    _p(f"watching {root} every {args.interval:g}s" + (" and deploying to the Quest" if args.deploy else "")
       + "  (Ctrl-C to stop)")
    last = None
    pending_deploy = args.deploy
    try:
        while True:
            snap = snapshot()
            if snap != last:
                time.sleep(args.interval)                   # let MuseScore finish writing
                if snapshot() != snap:
                    continue
                rep = lib.build(jobs=args.jobs)
                if last is not None or rep.written or rep.retired or rep.moved or rep.adopted or rep.failed:
                    _p(time.strftime("[%H:%M:%S] ") + f"{len(rep.written)} written, {len(rep.retired)} retired, "
                       f"{len(rep.failed)} failed")
                    for r, o, why in rep.written:
                        _p(f"  {o}  <- {r} ({why})")
                    for r, e in rep.failed:
                        _p(f"  FAILED {r}: {e}")
                if rep.written or rep.retired or rep.moved:
                    pending_deploy = args.deploy
                last = snap
            if pending_deploy:
                try:
                    adb = Adb(lib.cfg.adb, lib.cfg.serial)
                    plan = plan_deploy(lib, adb)
                    if plan.push or (args.prune and plan.remove):
                        execute_deploy(lib, adb, plan, prune=args.prune)
                        _p(time.strftime("[%H:%M:%S] ") + f"deployed {len(plan.push)} file(s) to {plan.model}")
                    pending_deploy = False
                except DeviceError:
                    pass                                    # headset not attached; try again later
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_verify(args) -> int:
    lib = _library(args)
    res = lib.verify(only=args.scores_ or None, calibrate=args.calibrate, jobs=args.jobs)
    counts = {}
    parts = {}
    for rel, status, r in res:
        counts[status] = counts.get(status, 0) + 1
        if "parts" in r:
            parts[r["parts"]] = parts.get(r["parts"], 0) + 1
        if status != "identical":
            extra = r.get("error") or r.get("compat") or ("same notes" if r.get("same_notes") else "notes differ")
            _p(f"  {status:10} {rel}  {extra}")
        if r.get("parts") == "differs":
            _p(f"  parts differ {rel}  (NoteWaterfall/ file; `build` rewrites it)")
    _p(", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    if parts:
        _p("Note Waterfall parts files: " + ", ".join(f"{v} {k}" for k, v in sorted(parts.items())))
    return 0 if counts.get("error", 0) == 0 else 1


def cmd_rename(args) -> int:
    lib = _library(args)
    done = lib.rename(only=args.scores_ or None, dry_run=args.dry_run)
    for rel, old, new in done:
        _p(f"  {old} -> {new}   ({rel})")
    _p(f"{len(done)} {'would be ' if args.dry_run else ''}renamed")
    return 0


def cmd_convert(args) -> int:
    from .convert import convert_mscz
    from .render import Compat
    from .smf import write_midi
    compat = Compat(dynamics=args.dynamics)
    c = convert_mscz(args.score, compat, not args.no_orchestra, not args.no_simplified, keep_midi=bool(args.midi),
                     provenance=bool(args.provenance), parts=bool(args.parts))
    out = args.out or os.path.join(os.getcwd(), c.name)
    with open(out, "wb") as f:
        f.write(c.data)
    if args.parts:
        from .convert import content_hash
        from .parts import parts_bytes
        with open(args.parts, "wb") as f:
            f.write(parts_bytes(c.parts, os.path.basename(out), c.data, content_hash(args.score)))
    if args.midi:
        write_midi(c.midi, args.midi)
    if args.provenance:
        import json
        with open(args.provenance, "w", encoding="utf-8") as f:
            for hand in ("right", "left"):
                for ns in c.provenance[hand]:
                    f.write(json.dumps(ns.to_dict()) + "\n")
    _p(f"{out}  ({c.title} / {c.artist})")
    return 0


def cmd_hands(args) -> int:
    import glob
    from . import handedits as H
    from .device import Adb, DeviceError
    lib = _library(args)
    serial = args.serial or lib.cfg.serial
    device_dir = args.device_dir or lib.cfg.hands_dir
    inbox = args.inbox or os.path.join(lib.out, ".hands", "inbox")
    adb = Adb(lib.cfg.adb, serial)

    if args.action == "pull" or args.pull:
        try:
            names = H.pull(adb, device_dir, inbox)
        except DeviceError as e:
            _p(f"pull: {e}")
            return 2
        _p(f"pulled {len(names)} hand-edit file(s) from {adb.serial}:{device_dir} into {inbox}")
        for n in names:
            _p(f"  {n}")
        if args.action == "pull":
            return 0

    files = args.sidecars or sorted(glob.glob(os.path.join(inbox, "*" + H.SIDECAR_EXT)))
    if not files:
        _p(f"no hand-edit files in {inbox} (run `hands pull` with the headset attached)")
        return 0
    _p(f"reviewing {len(files)} hand-edit file(s) ...")
    reviews = H.review_all(lib, files, all_repeats=args.all_repeats, log=_p if args.verbose else (lambda s: None))
    for rv in reviews:
        _p("")
        for line in H.format_review(rv):
            _p(line)
    ready = [rv for rv in reviews if rv.ready]
    _p("")
    _p(f"{len(ready)} score(s) can be updated, "
       f"{sum(rv.moved_notes for rv in ready)} note(s) change hands")
    if args.action == "review" or not ready:
        return 0

    if not args.yes and not sys.stdin.isatty():
        _p("refusing to overwrite scores without a terminal to ask; pass --yes")
        return 1
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    deploy_pv, deploy_nw, archive = hands_callbacks(
        lib, adb, device_dir, reviews, stamp, deploy=not args.no_deploy, archive=not args.no_archive,
        waterfall_deploy=args.waterfall_deploy)
    rep = H.apply_reviews(lib, reviews, yes=args.yes, deploy_pianovision=deploy_pv, deploy_waterfall=deploy_nw,
                          archive=archive)
    _list("declined", rep.declined)
    _list("scores overwritten", rep.written)
    _list("backups", rep.backups)
    _list("songs rebuilt", rep.built)
    _list("hand-edit files moved to applied/ on the headset", rep.archived)
    hands_archive_local(lib, inbox, rep.archived, stamp)
    _list("FAILED", [f"{r}: {e}" for r, e in rep.failed], 50)
    for m in rep.messages:
        _p(m)
    return 1 if rep.failed else 0


def hands_callbacks(lib: Library, adb, device_dir: str, reviews, stamp: str, deploy: bool = True,
                    archive: bool = True, waterfall_deploy: Optional[str] = None, run=None):
    """The deploy / archive steps of ``hands apply`` (shared with the GUI): returns
    ``(deploy_pianovision, deploy_waterfall, archive)`` for :func:`handedits.apply_reviews`, each
    None when skipped.  ``run`` runs Note Waterfall's deploy command (default: subprocess.run)."""
    import posixpath
    import shlex
    import subprocess
    from . import handedits as H
    from .device import execute_deploy, plan_deploy
    deploy_pv = deploy_nw = archive_fn = None
    if not deploy:
        return deploy_pv, deploy_nw, archive_fn

    def deploy_pv(outs):
        import dataclasses
        plan = plan_deploy(lib, adb)
        plan = dataclasses.replace(plan, push=[n for n in plan.push if n in outs],
                                   local={n: h for n, h in plan.local.items() if n in outs})
        execute_deploy(lib, adb, plan)
        _p(f"  PianoVision: {len(plan.push)} song(s) pushed")

    cmd = waterfall_deploy if waterfall_deploy is not None else lib.cfg.waterfall_deploy
    if cmd:
        def deploy_nw(outs):
            argv = shlex.split(cmd) + ["--library", lib.out]
            if adb.serial:
                argv += ["--serial", adb.serial]
            for n in outs:
                argv += ["--only", H.glob_literal(n)]          # --only takes a glob
            r = (run or subprocess.run)(argv)
            if r.returncode != 0:
                raise RuntimeError(f"{' '.join(argv[:2])} exited with {r.returncode}")
    else:
        _p("  (no [hands] waterfall_deploy command configured: Note Waterfall is not deployed)")
    if archive and deploy_nw is not None:
        by_name = {sc.name: sc for rv in reviews for sc in rv.sidecars}
        songs_dir = posixpath.join(posixpath.dirname(device_dir.rstrip("/")), "Songs")

        def archive_fn(names):           # only once Note Waterfall has the rebuilt songs
            md5 = {}
            for n in names:
                song = by_name[n].song
                with open(lib.out_path(song), "rb") as f:
                    md5[song] = H._md5(f.read())
            ok = H.archivable(adb, device_dir, songs_dir, [by_name[n] for n in names], md5, log=_p)
            return H.archive_on_device(adb, device_dir, ok, stamp)
    return deploy_pv, deploy_nw, archive_fn


def hands_archive_local(lib: Library, inbox: str, archived, stamp: str) -> None:
    """The local copies of sidecars archived on the headset follow them to .hands/applied/<stamp>."""
    if not archived:
        return
    done = os.path.join(lib.out, ".hands", "applied", stamp)
    os.makedirs(done, exist_ok=True)
    for n in archived:
        if os.path.exists(os.path.join(inbox, n)):
            os.replace(os.path.join(inbox, n), os.path.join(done, n))


def cmd_gui(args) -> int:
    from .gui import run_gui
    return run_gui(args)


# ------------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="pianovision", description="MuseScore library -> PianoVision JSON -> Quest")
    ap.add_argument("--config", help="pianovision.toml (default: ./pianovision.toml or next to the package)")
    ap.add_argument("--scores", help="MuseScore scores folder (overrides the config)")
    ap.add_argument("--output", help="PianoVision JSON folder (overrides the config)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def jobs(p):
        p.add_argument("-j", "--jobs", type=int, default=None, help="parallel renders (default: CPU count)")

    def device(p):
        p.add_argument("--serial", help="adb device serial (default: the attached Quest)")
        p.add_argument("--dry-run", action="store_true", help="show what would be copied/removed")
        p.add_argument("--force-push", action="store_true", help="push every file even if already identical")
        p.add_argument("--prune", action="store_true",
                       help="remove files this tool deployed earlier that left the library (asks first)")
        p.add_argument("-y", "--yes", action="store_true", help="do not ask before removing (with --prune)")

    p = sub.add_parser("status", help="what would change, and library health")
    p.add_argument("--device", action="store_true", help="also compare with the attached Quest")
    p.add_argument("--serial")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("build", help="render new/edited scores, retire removed ones")
    p.add_argument("--force", nargs="*", metavar="SCORE",
                   help="re-render these scores (or all, with no names) even if unchanged")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--prune-orphans", action="store_true", help="move outputs without a score to the attic")
    p.add_argument("--report", metavar="FILE", help="also write what the build did (failed scores...) as JSON")
    jobs(p)
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("deploy", help="copy the library to the Quest")
    device(p)
    p.set_defaults(fn=cmd_deploy)

    p = sub.add_parser("sync", help="build, then deploy")
    device(p)
    p.add_argument("--report", metavar="FILE", help="also write what the build did as JSON")
    jobs(p)
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("watch", help="rebuild (and deploy) whenever scores change")
    p.add_argument("--interval", type=float, default=5.0, help="seconds between checks")
    p.add_argument("--deploy", action="store_true", help="also deploy when the Quest is attached")
    p.add_argument("--prune", action="store_true", help="also remove retired files from the Quest")
    p.add_argument("--serial")
    jobs(p)
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("verify", help="re-render and compare with the pinned outputs")
    p.add_argument("scores_", nargs="*", metavar="SCORE")
    p.add_argument("--calibrate", action="store_true",
                   help="for differences, search the MuseScore-version knobs and remember them")
    jobs(p)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("rename", help="re-name outputs whose score title changed")
    p.add_argument("scores_", nargs="*", metavar="SCORE",
                   help="also apply the current naming rules to these (e.g. MuseScore 3 scores)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_rename)

    p = sub.add_parser("convert", help="convert one score (no library)")
    p.add_argument("score")
    p.add_argument("-o", "--out", help="output .json (default: ./<auth>_<title>.json)")
    p.add_argument("--midi", help="also write the rendered MIDI here")
    p.add_argument("--dynamics", choices=("4.6", "4.5"), default="4.6", help="MuseScore dynamics model")
    p.add_argument("--no-orchestra", action="store_true")
    p.add_argument("--no-simplified", action="store_true")
    p.add_argument("--provenance", metavar="FILE",
                   help="also write, per tracksV2 note, its source measure/staff/voice/chord/note (JSON lines)")
    p.add_argument("--parts", metavar="FILE",
                   help="also write Note Waterfall's parts file (original / simplified piano part, orchestra)")
    p.set_defaults(fn=cmd_convert)

    p = sub.add_parser("hands", help="hand edits recorded on the headset -> the scores (pull | review | apply)")
    p.add_argument("action", choices=("pull", "review", "apply"),
                   help="pull: copy the sidecars from the headset; review: show what would change (writes "
                        "nothing); apply: review, then per score y/N, back up, overwrite, build, deploy")
    p.add_argument("sidecars", nargs="*", metavar="FILE", help="*.hands.json files (default: the inbox)")
    p.add_argument("--pull", action="store_true", help="pull from the headset first (review/apply)")
    p.add_argument("--inbox", help="folder for pulled sidecars (default: <output>/.hands/inbox)")
    p.add_argument("--device-dir", help="sidecar folder on the headset (default: [hands] device_dir)")
    p.add_argument("--serial", help="adb device serial (default: the attached Quest)")
    p.add_argument("--all-repeats", action="store_true",
                   help="a note recorded on only some passes of a repeat changes hands on all of them")
    p.add_argument("-y", "--yes", action="store_true", help="apply without asking per score")
    p.add_argument("--no-deploy", action="store_true", help="apply: do not deploy or archive (build only)")
    p.add_argument("--no-archive", action="store_true",
                   help="apply: leave the sidecars on the headset (they are archived to applied/ otherwise)")
    p.add_argument("--waterfall-deploy", metavar="CMD",
                   help="command that deploys songs to Note Waterfall (default: [hands] waterfall_deploy)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_hands)

    p = sub.add_parser("gui", help="the Piano Library window: library, build, push to the headset, hand edits")
    p.add_argument("--read-only", action="store_true",
                   help="only show the library: every action that writes (build, push, rename, hand edits) is off")
    p.add_argument("--no-device", action="store_true", help="do not talk to adb (no device status, no push)")
    p.add_argument("--screenshot", metavar="PNG",
                   help="after the first refresh, save the window as PNG and quit (used to check the launcher)")
    p.set_defaults(fn=cmd_gui)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (FileNotFoundError, KeyError) as e:
        _p(f"error: {e}")
        return 2
