import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUFF = Path(sys.executable).parent / "ruff"  # installed next to the venv's python


def lint_src(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            RUFF,
            "check",
            "--select",
            "TID251",
            "--stdin-filename",
            "src/pricing_watch/example.py",
            "-",
        ],
        input=code,
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


@pytest.mark.parametrize(
    "code",
    [
        "from datetime import datetime\ndatetime.now()\n",
        "import datetime\ndatetime.date.today()\n",
        "import time\ntime.time()\n",
        "import random\n",
        "from random import choice\n",
        "import numpy as np\nnp.random.default_rng()\n",
        "import numpy as np\nnp.random.normal()\n",
        "import uuid\nuuid.uuid4()\n",
    ],
)
def test_wall_clock_and_random_rejected_in_src(code: str) -> None:
    result = lint_src(code)
    assert result.returncode == 1, result.stdout
    assert "TID251" in result.stdout


def test_allowed_code_passes() -> None:
    result = lint_src("from datetime import datetime, timedelta\nx = timedelta(hours=1)\n")
    assert result.returncode == 0, result.stdout
