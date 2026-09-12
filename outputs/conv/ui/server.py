# -*- coding: utf-8 -*-
"""本地网页控制台的 HTTP 服务（只在 127.0.0.1 监听，带一次性访问令牌）。

接口一览::

    GET  /                     主界面
    GET  /api/state            引擎 + 插件状态
    POST /api/add?name=X       上传一个文件 → 返回它的可转目标
    POST /api/convert          执行转换 {ids, dst, action, opts, out}
    POST /api/drop             清掉临时文件 {id} 或 {ids}
    POST /api/pick             系统「选择文件夹」对话框
    POST /api/pick-cancel      强关卡住的对话框
    POST /api/reveal           在资源管理器里打开输出目录
    POST /api/bye              页面关了 → 准备退出

「关浏览器就退出」这条链路是踩坑最多的，三个坑都在下面标了注释：
刷新页面被误判成退出、转换途中被强退、只杀父进程留下残留。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..core import engines
from ..core.registry import get_registry
from . import dialogs
from .page import PAGE

MAX_UPLOAD = 2 << 30            # 2GB，别再大了
SESSION_PREFIX = "conv_ui_"
ADDR_FILE = Path(tempfile.gettempdir()) / "conv_ui_addr.txt"
BYE_GRACE = 5.0                 # 收到 bye 后等几秒 —— 用户可能只是在刷新


def _clean_old_sessions() -> None:
    """清掉上次崩溃残留的会话目录（超过 1 小时的）。"""
    tmp = Path(tempfile.gettempdir())
    now = time.time()
    try:
        for d in tmp.glob(SESSION_PREFIX + "*"):
            try:
                if d.is_dir() and now - d.stat().st_mtime > 3600:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def default_output_dir() -> Path:
    home = Path.home()
    for cand in (home / "Desktop", home / "桌面", home):
        if cand.is_dir():
            return cand / "格式转换输出"
    return home


def serve(port: int = 0, open_browser: bool = True,
          out_hint: str | None = None, app_mode: bool = True) -> int:
    _clean_old_sessions()

    token = secrets.token_urlsafe(12)
    session = Path(tempfile.mkdtemp(prefix=SESSION_PREFIX))
    up_dir = session / "in"
    up_dir.mkdir()
    fallback_dir = session / "out"
    fallback_dir.mkdir()

    reg = get_registry()
    lock = threading.Lock()
    state = {"busy": 0, "bye": 0.0, "loaded": False}
    uploads: dict[str, tuple[Path, str]] = {}

    default_out = Path(out_hint) if out_hint else default_output_dir()

    def resolve_out(raw: str):
        """解析输出目录；不可写就退到临时目录，并如实告诉调用方。"""
        p = (raw or "").strip().strip('"').strip("'")
        cand = Path(p) if p else default_out
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / f".conv_probe_{os.getpid()}"
            probe.write_bytes(b"")
            probe.unlink(missing_ok=True)
            return cand, False
        except Exception:                                    # noqa: BLE001
            return fallback_dir, True

    # ------------------------------------------------------------------
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ConvWorkbench/1.1"

        def log_message(self, *a):                           # 静音访问日志
            pass

        # ---------- 基础 ----------
        def _send(self, code, body: bytes,
                  ctype="text/plain; charset=utf-8", extra=None):
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass                                        # 客户端先走了，正常

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            buf = bytearray()
            while len(buf) < n:
                chunk = self.rfile.read(min(1 << 20, n - len(buf)))
                if not chunk:
                    break
                buf += chunk
            return bytes(buf)

        def _json_body(self) -> dict:
            try:
                return json.loads(self._body() or b"{}")
            except Exception:                                # noqa: BLE001
                return {}

        def _parts(self):
            parsed = urllib.parse.urlparse(self.path)
            seg = parsed.path.strip("/").split("/", 1)
            tok = seg[0] if seg else ""
            rest = seg[1] if len(seg) > 1 else ""
            return tok, rest, urllib.parse.parse_qs(parsed.query)

        def _guard(self) -> bool:
            tok, _, _ = self._parts()
            if not secrets.compare_digest(tok, token):
                self._json({"ok": False, "msg": "访问令牌无效"}, 403)
                return False
            return True

        # ---------- 路由 ----------
        def do_GET(self):
            if not self._guard():
                return
            _, rest, _q = self._parts()
            if rest in ("", "index.html"):
                with lock:
                    state["loaded"] = True
                    # 页面重新加载 = 用户还在用，撤销待退出。
                    # 没有这一步，用户按 F5 刷新会被当成"关窗"而误退出。
                    state["bye"] = 0.0
                page = (PAGE.replace("__OUTDIR__", _html_attr(default_out))
                            .replace("__TOKEN__", token))
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif rest == "api/state":
                self._do_state()
            elif rest == "api/capabilities":
                self._do_capabilities()
            else:
                self._json({"ok": False, "msg": "未知接口"}, 404)

        def do_POST(self):
            if not self._guard():
                return
            _, rest, q = self._parts()
            {
                "api/add": lambda: self._do_add(q),
                "api/convert": self._do_convert,
                "api/drop": self._do_drop,
                "api/pick": self._do_pick,
                "api/pick-cancel": self._do_pick_cancel,
                "api/reveal": self._do_reveal,
                "api/bye": self._do_bye,
            }.get(rest, lambda: self._json({"ok": False, "msg": "未知接口"}, 404))()

        # ---------- 状态 ----------
        def _do_capabilities(self):
            """能力总览：按类别分板块，供界面的「支持的功能」导航用。"""
            self._json({"groups": reg.overview()})

        def _do_state(self):
            engs = list(engines.status().values())
            plugs = []
            for c in reg.converters:
                ok, why = c.available()
                plugs.append({"label": c.label, "ok": ok, "reason": why})
            self._json({"engines": engs, "plugins": plugs,
                        "src_exts": reg.src_exts(), "dst_exts": reg.dst_exts()})

        # ---------- 上传 ----------
        def _do_add(self, q):
            raw_name = (q.get("name") or ["upload"])[0]
            name = Path(raw_name).name            # 去掉任何目录成分，防穿越
            n = int(self.headers.get("Content-Length") or 0)

            if n <= 0:
                self._json({"ok": False, "msg": "收到空文件"})
                return
            if n > MAX_UPLOAD:
                self._json({"ok": False, "msg": "文件超过 2GB 上限"})
                return

            ext = Path(name).suffix.lower().lstrip(".")
            uid = uuid.uuid4().hex[:12]
            dest = up_dir / (uid + ("." + ext if ext else ""))

            got = 0
            try:
                with open(dest, "wb") as f:
                    while got < n:
                        chunk = self.rfile.read(min(1 << 20, n - got))
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
            except Exception as e:                           # noqa: BLE001
                dest.unlink(missing_ok=True)
                self._json({"ok": False, "msg": f"接收失败：{e}"})
                return

            if got != n:
                dest.unlink(missing_ok=True)
                self._json({"ok": False, "msg": f"上传不完整（{got}/{n} 字节）"})
                return

            with lock:
                uploads[uid] = (dest, name)

            targets = [t.to_dict() for t in reg.targets_for(ext)]
            self._json({"ok": True, "id": uid, "name": name, "size": got,
                        "ext": ext, "targets": targets})

        # ---------- 转换 ----------
        def _do_convert(self):
            req = self._json_body()
            ids = req.get("ids") or []
            dst = str(req.get("dst") or "")
            action = req.get("action") or None
            opts = req.get("opts") or {}
            outdir, fell_back = resolve_out(str(req.get("out") or ""))

            srcs: list[Path] = []
            name_of: dict[Path, str] = {}
            with lock:
                for i in ids:
                    got = uploads.get(i)
                    if got:
                        srcs.append(got[0])
                        name_of[got[0]] = got[1]

            if not srcs:
                self._json({"ok": False, "out": str(outdir), "results": [
                    {"ok": False, "name": "", "msg": "文件缓存已失效，请重新拖进来",
                     "src": "", "size": 0}]})
                return

            with lock:
                state["busy"] += 1
            try:
                results = reg.run(srcs, dst, outdir, opts, action)
            finally:
                # 转换途中不许被"关窗"打断 —— busy 减回 0 之后 watchdog 才会动手
                with lock:
                    state["busy"] -= 1

            payload = []
            for r in results:
                d = r.to_dict()
                if r.src is not None and r.src in name_of:
                    d["src"] = name_of[r.src]     # 显示原始文件名，不是临时名
                payload.append(d)

            self._json({"ok": any(r.ok for r in results),
                        "results": payload, "out": str(outdir),
                        "fallback": fell_back})

        # ---------- 清理 ----------
        def _do_drop(self):
            req = self._json_body()
            ids = req.get("ids")
            if not ids:
                ids = [req["id"]] if req.get("id") else []

            n = 0
            with lock:
                for i in ids:
                    got = uploads.pop(i, None)
                    if got:
                        try:
                            got[0].unlink(missing_ok=True)
                            n += 1
                        except OSError:
                            pass
            self._json({"ok": True, "n": n})

        # ---------- 对话框 ----------
        def _do_pick(self):
            req = self._json_body()
            if dialogs.has_open_pick():
                self._json({"ok": False, "busy": True,
                            "msg": "已经有一个选择窗口开着了，先处理那个"})
                return
            okk, res = dialogs.pick_folder(str(req.get("path") or ""),
                                           "选择转换后文件的保存位置")
            if okk:
                self._json({"ok": True, "path": res})
            else:
                self._json({"ok": False, "msg": res})

        def _do_pick_cancel(self):
            self._json({"ok": True, "killed": dialogs.cancel_pick()})

        # ---------- 打开目录 / 退出 ----------
        def _do_reveal(self):
            req = self._json_body()
            target = str(req.get("path") or "").strip()
            d = Path(target) if target else default_out
            try:
                if d.is_file():
                    if os.name == "nt":
                        subprocess.Popen(["explorer", "/select,", str(d)])
                    else:
                        subprocess.Popen(["xdg-open", str(d.parent)])
                else:
                    d.mkdir(parents=True, exist_ok=True)
                    if os.name == "nt":
                        os.startfile(str(d))                 # noqa: S606
                    else:
                        subprocess.Popen(["xdg-open", str(d)])
                self._json({"ok": True, "path": str(d)})
            except Exception as e:                           # noqa: BLE001
                self._json({"ok": False, "msg": f"打不开目录：{e}"})

        def _do_bye(self):
            with lock:
                state["bye"] = time.time()
            self._json({"ok": True})

    # ------------------------------------------------------------------
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    actual = httpd.server_address[1]
    url = f"http://127.0.0.1:{actual}/{token}/"

    def watchdog():
        """盯着「页面关了」这个信号。

        只有同时满足三条才退出：收到过 bye、页面成功加载过、当前没有转换在跑。
        loaded 这条防的是"程序刚起来、页面还没打开就退出"；
        busy 这条防的是"转换跑到一半被强杀"。
        """
        while True:
            time.sleep(0.5)
            with lock:
                bye = state["bye"]
                busy = state["busy"]
                loaded = state["loaded"]
            if bye and loaded and not busy and (time.time() - bye) > BYE_GRACE:
                try:
                    httpd.shutdown()
                except Exception:                            # noqa: BLE001
                    pass
                return

    threading.Thread(target=watchdog, daemon=True).start()

    if open_browser:
        try:
            from ncm2mp3 import find_app_browser, open_app_window
            browser = find_app_browser() if app_mode else None
            if browser:
                open_app_window(url, browser)
            else:
                import webbrowser
                webbrowser.open(url)
        except Exception:                                    # noqa: BLE001
            import webbrowser
            try:
                webbrowser.open(url)
            except Exception:                                # noqa: BLE001
                pass

    try:
        ADDR_FILE.write_text(url, encoding="utf-8")
    except OSError:
        pass

    print(f"格式转换工作台已启动：{url}")
    print("（关掉那个窗口就会退出）")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.server_close()
        except Exception:                                    # noqa: BLE001
            pass
        shutil.rmtree(session, ignore_errors=True)
        try:
            ADDR_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


def _html_attr(s: object) -> str:
    """放进 HTML 属性里的转义。"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
