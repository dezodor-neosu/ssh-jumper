"""Copy installed dependency license notices into the portable distribution."""
from importlib.metadata import distributions
from pathlib import Path
import shutil
import sys

bundle = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "dist" / "SSH-Jumper"
destination = bundle / "licenses"
destination.mkdir(parents=True, exist_ok=True)
for distribution in distributions():
    for file in distribution.files or []:
        if any(word in file.name.lower() for word in ("license", "copying", "notice")):
            source = Path(distribution.locate_file(file))
            if source.is_file() and source.suffix.lower() not in (".py", ".pyc", ".pyd", ".dll"):
                target = destination / distribution.metadata["Name"] / Path(str(file))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.exists():
    shutil.copy2(python_license, destination / "Python-LICENSE.txt")
