"""实测：把候选库捆进 onefile exe 后，体积会涨多少。

做法：先用一个空的 main 打一个基线 exe，再逐个加库打，
      差值就是该库的真实体积代价（PyInstaller 会自行裁剪）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PY = sys.executable
WORK = Path(tempfile.mkdtemp(prefix="probe_"))
print(f"临时目录：{WORK}\n")

CASES = {
    "baseline": "",
    "pillow": "import PIL.Image, PIL.ImageOps, PIL.ImageDraw\n",
    "pypdf": "import pypdf\n",
    "pypdfium2": "import pypdfium2\n",
    "python-docx": "import docx\n",
    "pillow+pypdf+pypdfium2": (
        "import PIL.Image, PIL.ImageOps, PIL.ImageDraw\n"
        "import pypdf\n"
        "import pypdfium2\n"
    ),
}

sizes: dict[str, int] = {}

for name, imports in CASES.items():
    d = WORK / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "m.py").write_text(imports + "print('ok')\n", encoding="utf-8")
    out = d / "out"
    cmd = [
        PY, "-m", "PyInstaller", "--noconfirm", "--onefile", "--console",
        "--name", "probe", "--distpath", str(out),
        "--workpath", str(d / "w"), "--specpath", str(d / "w"),
        str(d / "m.py"),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    exe = out / "probe.exe"
    if exe.is_file():
        sizes[name] = exe.stat().st_size
        print(f"{name:24} {exe.stat().st_size/1048576:8.1f} MB")
    else:
        print(f"{name:24} 打包失败")
        print((r.stderr or r.stdout)[-400:])

base = sizes.get("baseline", 0)
print(f"\n{'—'*46}")
print(f"{'基线（空程序）':24} {base/1048576:8.1f} MB")
for k, v in sizes.items():
    if k == "baseline":
        continue
    print(f"{k:24} 增量 {(v-base)/1048576:+8.1f} MB   合计 {v/1048576:7.1f} MB")

shutil.rmtree(WORK, ignore_errors=True)
