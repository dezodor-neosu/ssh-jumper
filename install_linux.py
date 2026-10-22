"""Install a per-user launcher for this checkout's virtual environment."""
import os
from pathlib import Path
import shlex
import sys


def install():
    root = Path(__file__).resolve().parent
    interpreter = root / '.venv' / 'bin' / 'python'
    if not interpreter.is_file():
        raise SystemExit('Create .venv and install requirements.txt first.')
    destination = Path.home() / '.local' / 'bin' / 'ssh-jumper'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text('#!/bin/sh\nexec ' + shlex.quote(str(interpreter)) + ' ' +
                           shlex.quote(str(root / 'main.py')) + ' "$@"\n')
    destination.chmod(0o755)
    applications = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'applications'
    applications.mkdir(parents=True, exist_ok=True)
    # Desktop Exec quoting differs from shell quoting.
    escaped = str(destination).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
    (applications / 'ssh-jumper.desktop').write_text(
        '[Desktop Entry]\nType=Application\nName=SSH Jumper\n'
        'Comment=SSH tunnels, terminals and file transfers\n'
        f'Exec="{escaped}"\nIcon=network-server\nTerminal=false\nCategories=Network;RemoteAccess;\n')
    print(f'Installed {destination}')
    print('SSH Jumper is now available in the application menu. Keep this checkout in place.')


if __name__ == '__main__':
    install()
