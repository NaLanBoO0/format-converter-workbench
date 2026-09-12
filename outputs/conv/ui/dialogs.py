# -*- coding: utf-8 -*-
"""系统「选择文件夹」对话框（Windows）。

这段代码看着简单，但里面每一条都是踩过坑换来的，别随手改：

1. **必须指定一个 TopMost 的 owner 窗口。**
   对话框是后台子进程（PowerShell）建的，子进程不是前台进程。
   Windows 有前台锁 —— 非前台进程没法把窗口抢到最前。
   不指定 owner 时，``ShowDialog()`` 的对话框会开在窗口**后面**，
   用户看到的现象是「点了按钮变成选择中，但没弹窗」，然后被一个
   禁用的按钮卡死。owned window 永远压在自己 owner 之上，
   owner 又是 TopMost，所以对话框必然在最前。

2. **路径用 UTF-8 Base64 回传。**
   重定向 stdout 时 PowerShell 默认按 OEM 代码页输出，中文目录名会
   解码失败。而且无控制台的子进程里 ``[Console]::OutputEncoding``
   可能直接抛异常 —— 所以连那行都别写，直接 Base64。

3. **给足超时 + 支持强制取消。**
   对话框是模态的，可能一直开着。用户万一看不到窗口，得有个出路。
"""
from __future__ import annotations

import base64
import os
import subprocess
import threading

_current: "subprocess.Popen | None" = None
_lock = threading.Lock()


def _ps_quote(s: str) -> str:
    """放进 PowerShell 单引号字符串里 —— 去掉单引号防注入。"""
    return str(s).replace("'", "").replace("\r", " ").replace("\n", " ")[:60]


def pick_folder(seed: str = "", desc: str = "选择文件夹",
                timeout: int = 1800) -> tuple[bool, str]:
    """弹出系统文件夹选择框。

    返回 ``(是否选到, 路径或原因)``。用户取消时返回 ``(False, "没有选择目录")``。
    """
    global _current

    if os.name != "nt":
        return False, "只有 Windows 支持系统对话框，请直接手填路径"

    seed = (seed or "").strip()
    seed_ps = (
        "if (Test-Path -LiteralPath $env:CONV_PICK_SEED -PathType Container)"
        " { $d.SelectedPath = $env:CONV_PICK_SEED };"
        if seed else ""
    )

    ps = (
        "$ErrorActionPreference = 'Stop';"
        "Add-Type -AssemblyName System.Windows.Forms;"
        # --- 1×1 的隐形 owner，TopMost 保证对话框在最前线 ---
        "$o = New-Object System.Windows.Forms.Form;"
        f"$o.Text = '{_ps_quote(desc)}';"
        "$o.FormBorderStyle = 'None';"
        "$o.ShowInTaskbar = $false;"
        "$o.StartPosition = 'CenterScreen';"
        "$o.Size = New-Object System.Drawing.Size(1,1);"
        "$o.TopMost = $true;"
        "$o.Show();"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        f"$d.Description = '{_ps_quote(desc)}';"
        "$d.ShowNewFolderButton = $true;"
        + seed_ps +
        "try {"
        "  if ($d.ShowDialog($o) -eq [System.Windows.Forms.DialogResult]::OK)"
        "  { [Convert]::ToBase64String("
        "      [System.Text.Encoding]::UTF8.GetBytes($d.SelectedPath)) | Write-Output }"
        "} finally { $o.Close(); $o.Dispose() }"
    )

    env = dict(os.environ)
    if seed:
        env["CONV_PICK_SEED"] = seed

    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=env,
        )
    except Exception as e:                                   # noqa: BLE001
        return False, f"调不起系统对话框：{e}"

    with _lock:
        _current = proc
    try:
        out, _err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        return False, "等待选择超时，请重新点一次"
    except Exception as e:                                   # noqa: BLE001
        return False, f"等待对话框出错：{e}"
    finally:
        with _lock:
            _current = None

    text = (out or "").strip()
    if not text:
        return False, "没有选择目录"

    b64 = text.splitlines()[-1].strip()
    try:
        return True, base64.b64decode(b64).decode("utf-8")
    except Exception:                                        # noqa: BLE001
        return False, "返回的路径解不出来"


def _kill(proc) -> None:
    """连子进程一起干掉。只杀父进程会留下残留的 powershell。"""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, text=True, timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:                                        # noqa: BLE001
        try:
            proc.kill()
        except Exception:                                    # noqa: BLE001
            pass


def cancel_pick() -> bool:
    """用户说"我没看到窗口" —— 把卡住的对话框干掉。"""
    with _lock:
        proc = _current
    if proc is None:
        return False
    _kill(proc)
    return True


def has_open_pick() -> bool:
    with _lock:
        return _current is not None
