#!/usr/bin/env python3
import os
import sys
import shutil
import subprocess
from typing import Optional

APPIMAGE_DEFAULT = "/home/john/.local/bin/MuseScore-Studio-4.6.0.252730944-x86_64.AppImage"

def _find_musescore_cli() -> Optional[str]:
    """
    Resolve MuseScore CLI executable.
    Priority:
      1) $MUSESCORE_CLI env var
      2) Known AppImage path
      3) Common command names on PATH
    """
    env_cmd = os.environ.get("MUSESCORE_CLI")
    if env_cmd and shutil.which(env_cmd):
        return env_cmd

    if os.path.isfile(APPIMAGE_DEFAULT):
        if not os.access(APPIMAGE_DEFAULT, os.X_OK):
            try:
                st = os.stat(APPIMAGE_DEFAULT)
                os.chmod(APPIMAGE_DEFAULT, st.st_mode | 0o111)
            except Exception as e:
                print(f"Warning: Could not chmod +x on MuseScore AppImage: {e}")
        return APPIMAGE_DEFAULT

    for cmd in ["musescore4", "mscore4", "musescore3", "mscore3", "mscore"]:
        if shutil.which(cmd):
            return cmd
    return None

def _export_mscz_to_midi(mscz_path: str, out_mid: str) -> bool:
    cli = _find_musescore_cli()
    if not cli:
        print("Warning: MuseScore CLI not found. Set MUSESCORE_CLI or place AppImage at expected path.")
        return False
    cmd = [cli, "-o", out_mid, mscz_path]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if res.returncode == 0 and os.path.isfile(out_mid):
            print(f"Generated MIDI: {out_mid}")
            return True
        print(f"Warning: MuseScore export failed ({res.returncode}) for {mscz_path}\n{res.stderr.decode(errors='ignore')}")
        return False
    except Exception as e:
        print(f"Warning: MuseScore export exception for {mscz_path}: {e}")
        return False

def needs_update(mscz: str, mid: str) -> bool:
    if not os.path.isfile(mid):
        return True
    return os.path.getmtime(mscz) > os.path.getmtime(mid)

def process(root_dir: str) -> int:
    updated = 0
    total = 0
    for dirpath, _, filenames in os.walk(root_dir):
        for name in filenames:
            if not name.lower().endswith(".mscz"):
                continue
            total += 1
            mscz_path = os.path.join(dirpath, name)
            mid_path = os.path.splitext(mscz_path)[0] + ".mid"
            if needs_update(mscz_path, mid_path):
                print(f"[Update] {os.path.relpath(mscz_path, root_dir)} -> {os.path.basename(mid_path)}")
                if _export_mscz_to_midi(mscz_path, mid_path):
                    updated += 1
            else:
                # Already current
                pass
    print(f"MIDI update summary: {updated} updated (out of {total} MSCZ files)")
    return 0

def main():
    if len(sys.argv) != 2:
        print("Usage: python update_midis.py <root_directory>")
        sys.exit(1)
    root = sys.argv[1]
    if not os.path.isdir(root):
        print(f"Error: '{root}' is not a directory")
        sys.exit(1)
    sys.exit(process(root))

if __name__ == "__main__":
    main()
