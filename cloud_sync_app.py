#!/usr/bin/env python3
r"""Cloud Sync entry point (built into CloudSync.exe). GUI with no args, CLI with args."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Single-instance marker: the installer's AppMutex checks this to warn the
# user to close the app before install/uninstall. Harmless off Windows.
if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.kernel32.CreateMutexW(None, False, "QuickOpen.CloudSync")
    except Exception:
        pass


#: Flags that belong to the GUI, not the CLI (autostart passes --minimized).
GUI_FLAGS = {"--minimized", "--tray"}


def main():
    argv = sys.argv[1:]
    if argv and set(argv) <= GUI_FLAGS:
        from cloudsync import gui
        return gui.main(argv) or 0
    if argv:
        from cloudsync import __main__ as cli
        if hasattr(cli, 'main'):
            try:
                return cli.main(argv)
            except TypeError:
                sys.argv = ['cloudsync', *argv]; return cli.main()
        sys.argv = ['cloudsync', *argv]
        import runpy; runpy.run_module('cloudsync', run_name='__main__'); return 0
    from cloudsync import gui
    return gui.main() or 0


if __name__ == '__main__':
    sys.exit(main() or 0)
