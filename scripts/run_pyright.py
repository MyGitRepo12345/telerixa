import os
import shutil
import subprocess
import sys


PYRIGHT_VERSION = "1.1.411"


def main():
    npx_name = "npx.cmd" if os.name == "nt" else "npx"
    npx_path = shutil.which(npx_name)
    if npx_path is None:
        print("ERROR: npx was not found. Install Node.js to run Pyright.")
        return 1

    pyright_args = [
        "--yes",
        f"pyright@{PYRIGHT_VERSION}",
        "--pythonpath",
        sys.executable,
        ".",
    ]
    command = [npx_path, *pyright_args]
    return subprocess.call(command, shell=os.name == "nt")


if __name__ == "__main__":
    raise SystemExit(main())
