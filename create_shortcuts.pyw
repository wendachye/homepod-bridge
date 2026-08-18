"""Double-click me once (Windows): creates 'HomePod Bridge' shortcuts.

- Startup folder -> the tray app launches automatically at every login
- Desktop -> double-click to launch it manually any time

Both run windowless pythonw, so no console ever appears. To undo, delete
'HomePod Bridge.lnk' from shell:startup and from the Desktop.

Run this AFTER moving the folder to its permanent location - the shortcuts
bake in the current folder path.
"""
from pathlib import Path
from tkinter import Tk, messagebox

from homepod_bridge.shortcuts import install


def main() -> None:
    root = Tk()
    root.withdraw()
    try:
        target, workdir = install(Path(__file__).resolve().parent)
        messagebox.showinfo(
            "HomePod Bridge",
            "Shortcuts created.\n\n"
            "- Starts automatically at every login\n"
            "- Desktop shortcut added for manual launch\n\n"
            f"Runs: {target} -m homepod_bridge tray\nIn: {workdir}\n\n"
            "Tip: enable 'Auto-connect at launch' in the tray menu so\n"
            "streaming resumes by itself after boot.\n\n"
            "To undo: delete 'HomePod Bridge.lnk' from shell:startup\n"
            "and from the Desktop.",
        )
    except Exception as exc:  # noqa: BLE001 - show the user, don't crash silently
        messagebox.showerror("HomePod Bridge", f"Could not create shortcuts:\n{exc}")
    finally:
        root.destroy()


main()
