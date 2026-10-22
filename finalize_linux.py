"""Check packaged GUI startup and single-instance exclusion with isolated settings."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def verify(bundle):
    application = Path(bundle).resolve() / 'SSH-Jumper'
    with tempfile.TemporaryDirectory(prefix='ssh-jumper-startup-') as folder:
        environment = dict(os.environ, XDG_CONFIG_HOME=folder)
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen([str(application)], env=environment, stderr=errors)
            try:
                time.sleep(2)
                if process.poll() is not None:
                    errors.seek(0)
                    raise RuntimeError('Packaged GUI exited during startup: ' + errors.read().decode(errors='replace'))
                duplicate = subprocess.run([str(application)], env=environment, timeout=10)
                if duplicate.returncode != 0:
                    raise RuntimeError('Duplicate instance did not exit successfully')
                if process.poll() is not None:
                    raise RuntimeError('Original instance exited during duplicate check')
                print('Packaged GUI startup and single-instance check passed')
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == '__main__':
    verify(sys.argv[1] if len(sys.argv) > 1 else 'dist/SSH-Jumper')
