import subprocess
from pathlib import Path

import pytest


def test_no_personal_data_in_tracked_files() -> None:
    markers = Path("storage/profiles/private_markers.txt")
    if not markers.exists():
        pytest.skip("no local markers")
    tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                             check=True).stdout.split("\n")
    words = [w.strip().lower() for w in markers.read_text().splitlines() if w.strip()]
    hits = [(f, w) for f in tracked if f and Path(f).is_file()
            for w in words if w in Path(f).read_text(errors="ignore").lower()]
    assert hits == []
