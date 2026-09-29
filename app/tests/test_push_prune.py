"""push.prune_cmd: old staging dirs go, the one just made live never does — even
when rsync -a gave it an older mtime than the previous deploys'."""

import os
import subprocess
import sys

import pytest
from deploy_engine.push import prune_cmd


@pytest.mark.skipif(sys.platform == "win32", reason="runs the remote shell locally")
def test_keeps_newest_by_name_and_the_live_one(tmp_path):
    names = [f"staging-20260929-{t}" for t in ("125218", "131927", "133042", "161951")]
    for n in names:
        (tmp_path / n).mkdir()
    os.utime(tmp_path / names[-1], (0, 0))  # the new one looks oldest to `ls -t`
    subprocess.run(["bash", "-c", prune_cmd(str(tmp_path / names[-1]), 2)], check=True)
    assert sorted(os.listdir(tmp_path)) == names[-2:]
