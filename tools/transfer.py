#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实例 ↔ 本机 的文件传输层（替代裸 scp），针对图片、视频、模型清单做了优化。

能力
  push(local, remote_dir)                  上传（tar 流 / scp 自适应）
  pull(remote_path, local_dir)             下载：目录/多文件/单文件自适应
  pull_file(remote, local, resume=True)    单文件断点续传（大视频用）
  verify(remote_paths)                     远端 sha256 + 大小核对
  probe()                                  探测远端能力（rsync / tar / gzip）

策略（自动选择，可用 mode= 强制）
  1) 本机与远端都有 rsync → rsync -az --partial（增量 + 断点 + 压缩）
  2) 否则 → **tar 流**：ssh "tar -cf - …" | 本地 tar -x（单连接、无逐文件开销）
     · 文本类自动 gzip；图片/视频不压缩（省 CPU）
  3) 兜底 → 并发 scp（--workers，默认 4）

用法（Python）
    from tools.transfer import Transfer
    t = Transfer(cs, inst)
    t.pull("/root/vid_out", "reports/video")          # 目录
    t.pull("/root/ComfyUI/output/*.png", "reports/f")  # 多文件
"""
from __future__ import annotations

import fnmatch
import os
import posixpath
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TEXT_EXT = {".py", ".sh", ".json", ".txt", ".yaml", ".yml", ".md", ".csv", ".log", ".html"}
NO_COMPRESS = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".gif", ".safetensors",
               ".bin", ".pt", ".ckpt", ".zip", ".gz", ".7z"}


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f}{u}" if u != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}GB"


class Transfer:
    def __init__(self, cs, inst: dict, workers: int = 4, timeout: int = 900,
                 quiet: bool = False, cipher: str | None = "aes128-gcm@openssh.com"):
        self.cs = cs
        self.cipher = cipher
        self.inst = inst
        self.workers = workers
        self.timeout = timeout
        self.quiet = quiet
        self._cap: dict | None = None

    # ---------------------------------------------------------------- 基础
    def _cred(self):
        login = (self.cs.ssh_of(self.inst) or "").split()
        ip = self.cs.public_ip(self.inst)
        user, port = "root", "22"
        for i, tok in enumerate(login):
            if tok == "-p" and i + 1 < len(login):
                port = login[i + 1]
            if "@" in tok:
                user, ip = tok.split("@")[0] or "root", tok.split("@")[1] or ip
        return user, ip, port, self.cs.password_of(self.inst)

    def _ssh_base(self):
        user, ip, port, pw = self._cred()
        base = ["sshpass", "-p", pw, "ssh", "-p", str(port),
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15"]
        if self.cipher:
            base += ["-c", self.cipher]
        return base + [f"{user}@{ip}"]

    def _scp_base(self):
        user, ip, port, pw = self._cred()
        base = ["sshpass", "-p", pw, "scp", "-P", str(port),
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=10"]
        if self.cipher:
            base += ["-c", self.cipher]
        return base, f"{user}@{ip}"

    def ssh(self, cmd: str, timeout: int = 120, retries: int = 1) -> tuple[int, str]:
        """执行远端命令，返回 (rc, out)。

        P2 修复：捕获 TimeoutExpired，超时返回 (124, "TIMEOUT(Ns)") 而不是抛异常。
        调用方全部按 (rc, out) 使用，无人依赖异常（reports/p1/csctl-pull-目录探测bug.md）。
        满载实例上 ssh 抖动很常见，retries>1 时先重试一次再降级。
        """
        last = "TIMEOUT"
        for attempt in range(max(1, retries)):
            try:
                r = subprocess.run(self._ssh_base() + [cmd], capture_output=True, text=True,
                                   timeout=timeout)
                return r.returncode, (r.stdout or "") + (("ERR:" + r.stderr) if r.returncode else "")
            except subprocess.TimeoutExpired:
                last = f"TIMEOUT({timeout}s)"
                if attempt + 1 < max(1, retries):
                    time.sleep(3)
                    continue
            except OSError as e:
                return 127, f"OSError:{e}"
        return 124, last

    def probe(self) -> dict:
        if self._cap is not None:
            return self._cap
        rc, out = self.ssh("for c in rsync tar gzip sha256sum; do "
                           "command -v $c >/dev/null && echo \"$c=1\" || echo \"$c=0\"; done; "
                           "df -Pm /root | tail -1 | awk '{print \"free_mb=\"$4}'", timeout=30)
        cap = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                cap[k.strip()] = v.strip()
        cap["local_rsync"] = "1" if shutil.which("rsync") else "0"
        self._cap = cap
        return cap

    # ---------------------------------------------------------------- 上传
    def push(self, local: str, remote_dir: str, mode: str = "auto") -> bool:
        local_p = Path(local)
        cap = self.probe()
        t0 = time.time()
        if not local_p.exists():
            print(f"  ✗ 本地不存在: {local}")
            return False
        self.ssh(f"mkdir -p {posixpath.dirname(remote_dir.rstrip('/')) or '/'}")
        if local_p.is_dir():
            if mode in ("auto", "rsync") and cap.get("rsync") == "1" and cap.get("local_rsync") == "1":
                ok = self._rsync(f"{local_p}/", remote_dir, upload=True)
            else:
                ok = self._tar_push(local_p, remote_dir)
            if ok and not self.quiet:
                print(f"  ↑ {local_p.name}/ → {remote_dir}  "
                      f"{human(self._dir_size(local_p))} in {time.time()-t0:.1f}s")
            return ok
        if mode in ("auto", "rsync") and cap.get("rsync") == "1" and cap.get("local_rsync") == "1":
            ok = self._rsync(str(local_p), remote_dir, upload=True)
        else:
            base, host = self._scp_base()
            r = subprocess.run(base + [str(local_p), f"{host}:{remote_dir}"],
                               capture_output=True, text=True, timeout=self.timeout)
            ok = r.returncode == 0
        if not self.quiet:
            print(f"  ↑ {local_p.name} → {remote_dir}  {human(local_p.stat().st_size)}"
                  f" in {time.time()-t0:.1f}s {'ok' if ok else 'FAIL'}")
        return ok

    def _tar_push(self, local_dir: Path, remote_dir: str) -> bool:
        parent = local_dir.parent
        name = local_dir.name
        ssh = subprocess.Popen(self._ssh_base() + [f"mkdir -p {remote_dir} && tar -xf - -C {remote_dir}"],
                               stdin=subprocess.PIPE)
        tar = subprocess.Popen(["tar", "-cf", "-", "-C", str(parent), name], stdout=ssh.stdin)
        ssh.stdin.close()
        tar.wait()
        ssh.wait()
        return ssh.returncode == 0

    # ---------------------------------------------------------------- 下载
    def pull(self, remote: str, local_dir: str, mode: str = "auto") -> bool:
        """远端单文件 / 目录 / 通配符，一律拉到本地 local_dir。"""
        # P2：探测与分类也纳入兜底，超时不再整条 pull 崩（原来在 try 之外）
        try:
            cap = self.probe()
            Path(local_dir).mkdir(parents=True, exist_ok=True)
            kind, items, base = self._classify(remote)
        except Exception as e:
            print(f"  ✗ 远端探测失败（{type(e).__name__}: {e}），放弃本次 pull")
            return False
        t0 = time.time()
        if not items:
            print(f"  ✗ 远端没有匹配: {remote}")
            return False
        total_hint = self._remote_size(items, base)
        ok = False
        if kind == "file" and len(items) == 1:
            ok = self.pull_file(posixpath.join(base, items[0]) if base else items[0],
                                os.path.join(local_dir, posixpath.basename(items[0])),
                                mode=mode)
        elif mode in ("auto", "rsync") and cap.get("rsync") == "1" and cap.get("local_rsync") == "1":
            ok = self._rsync(remote, local_dir, upload=False)
        elif kind == "dir" and mode == "auto":
            # 先看目录里有多少文件：少量大文件用 scp -r（吞吐高），大量小文件用 tar（省往返）
            rc, listing = self.ssh(f"ls -1 {remote} | wc -l", timeout=60)
            try:
                nfiles = int((listing or "0").strip().splitlines()[-1])
            except Exception:
                nfiles = 0
            sz = self._remote_size([posixpath.basename(remote.rstrip("/"))],
                                   posixpath.dirname(remote.rstrip("/")))
            use_tar = nfiles >= 50 or (nfiles and sz and sz / max(nfiles, 1) < 262144)
            if use_tar:
                ok = self._tar_pull(items, base, local_dir, strip=True)
            else:
                base_cmd, host = self._scp_base()
                r = subprocess.run(base_cmd + ["-r", f"{host}:{remote}", local_dir.rstrip("/") + "/"],
                                   capture_output=True, text=True, timeout=self.timeout)
                ok = r.returncode == 0
                if ok:
                    self._flatten(local_dir)
                else:
                    self.print_fallback(r.stderr)
                    ok = self._tar_pull(items, base, local_dir, strip=True)
        elif kind == "dir" and len(items) == 1:
            # 目录：先列内容，多文件则分片并发
            rc, listing = self.ssh(f"ls -1 {posixpath.join(base, items[0])}", timeout=60)
            files = [x for x in listing.splitlines() if x]
            if len(files) >= 4 and self.workers > 1:
                ok = self._parallel_scp(files, posixpath.join(base, items[0]), local_dir)
            else:
                ok = self._tar_pull(items, base, local_dir)
        else:
            ok = self._tar_pull(items, base, local_dir)
        if not self.quiet:
            el = max(time.time() - t0, 0.01)
            print(f"  ↓ {len(items)} 项 → {local_dir}  {human(total_hint)} in {el:.1f}s "
                  f"({human(total_hint/el)}/s) {'ok' if ok else 'FAIL'}")
        return ok

    def pull_file(self, remote: str, local: str, resume: bool = True, mode: str = "auto") -> bool:
        """单文件下载，支持断点续传（远端 tail -c +N 拼接），适合大视频。"""
        local_p = Path(local)
        local_p.parent.mkdir(parents=True, exist_ok=True)
        cap = self.probe()
        if mode in ("auto", "rsync") and cap.get("rsync") == "1" and cap.get("local_rsync") == "1":
            return self._rsync(remote, str(local_p.parent), upload=False)
        offset = 0
        if resume and local_p.exists() and local_p.stat().st_size > 0:
            offset = self._remote_size([posixpath.basename(remote)], posixpath.dirname(remote))
            have = local_p.stat().st_size
            if have < offset:
                if not self.quiet:
                    print(f"    ↻ 续传 {posixpath.basename(remote)}: 已有 {human(have)} / {human(offset)}")
                cmd = f"tail -c +{have + 1} {remote}"
                with local_p.open("ab") as f:
                    p = subprocess.Popen(self._ssh_base() + [cmd], stdout=f)
                    p.wait()
                return self._same_size(remote, local_p)
            elif have == offset:
                if not self.quiet:
                    print(f"    = 已完整 {posixpath.basename(remote)} ({human(have)})，跳过")
                return True
        base, host = self._scp_base()
        try:
            r = subprocess.run(base + [f"{host}:{remote}", str(local_p)],
                               capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            print(f"    ⚠ scp 超时（{self.timeout}s），放弃该文件")
            return False
        return r.returncode == 0 and self._same_size(remote, local_p)

    @staticmethod
    def _flatten(local_dir: str) -> None:
        """scp -r 会把目录名再套一层，这里把内容提上来。"""
        d = Path(local_dir)
        subs = [x for x in d.iterdir() if x.is_dir()] if d.exists() else []
        if len(subs) == 1 and not any(x.is_file() for x in d.iterdir()):
            inner = subs[0]
            for f in list(inner.iterdir()):
                f.rename(d / f.name)
            inner.rmdir()

    def _tar_pull(self, items: list[str], base: str, local_dir: str, strip: bool = False) -> bool:
        """单连接 tar 流，避免逐文件 scp 的往返开销。"""
        compress = self._should_gzip(items)
        args = " ".join(f"-C {base} {self._q(i)}" if base else self._q(i) for i in items)
        remote_cmd = f"tar -cf - {args} | {'gzip -1' if compress else 'cat'}"
        p1 = subprocess.Popen(self._ssh_base() + [remote_cmd], stdout=subprocess.PIPE)
        if compress:
            p2 = subprocess.Popen(["gzip", "-dc"], stdin=p1.stdout, stdout=subprocess.PIPE)
            out = p2.stdout
        else:
            out = p1.stdout
        p3 = subprocess.Popen(["tar", "-xf", "-", "-C", local_dir]
                              + (["--strip-components=1"] if strip else []), stdin=out)
        if compress:
            p2.stdout.close()
        p1.stdout.close()
        p3.wait()
        p1.wait()
        return p3.returncode == 0 and p1.returncode == 0

    def _parallel_scp(self, files: list[str], base: str, local_dir: str) -> bool:
        """多文件：按 workers 分片并发 scp（实测比单流 tar 快）。"""
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        user, ip, port, pw = self._cred()
        groups = [files[i::self.workers] for i in range(self.workers)]
        base_cmd = ["sshpass", "-p", pw, "scp", "-P", str(port),
                    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "ConnectTimeout=10"]
        if self.cipher:
            base_cmd += ["-c", self.cipher]

        def worker(group):
            # 每个 worker 逐文件单连接（多源 scp 在某些 sshpass 版本下只传第一个）
            ok = True
            for f in group:
                args = base_cmd + [f"{user}@{ip}:{posixpath.join(base, f)}",
                                   local_dir.rstrip("/") + "/"]
                r = subprocess.run(args, capture_output=True, timeout=self.timeout)
                if r.returncode != 0:
                    ok = False
                    if not self.quiet:
                        print(f"    ✗ {f}: {(r.stderr or b'').decode()[-90:]}")
            return ok

        with ThreadPoolExecutor(max_workers=min(self.workers, len(files))) as ex:
            return all(ex.map(worker, groups))

    def _parallel_pull(self, files: list[str], base: str, local_dir: str) -> bool:
        """按 workers 分片，多连接 tar 取回（每片一个 ssh 连接）。"""
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        groups = [files[i::self.workers] for i in range(self.workers)]
        results = []

        def worker(group):
            if not group:
                return True
            args = " ".join(self._q(i) for i in group)
            p1 = subprocess.Popen(self._ssh_base() + [f"tar -cf - -C {base} {args}"],
                                  stdout=subprocess.PIPE)
            p3 = subprocess.Popen(["tar", "-xf", "-", "-C", local_dir], stdin=p1.stdout)
            p1.stdout.close()
            p3.wait()
            p1.wait()
            return p3.returncode == 0

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            for r in ex.map(worker, groups):
                results.append(r)
        return all(results)

    def print_fallback(self, err) -> None:
        if not self.quiet:
            msg = (err or "").strip().splitlines()
            if msg:
                print(f"    scp 失败，回退 tar: {msg[-1][:100]}")

    # ---------------------------------------------------------------- rsync
    def _rsync(self, src: str, dst: str, upload: bool) -> bool:
        user, ip, port, pw = self._cred()
        ssh_cmd = (f"sshpass -p {pw} ssh -p {port} -o StrictHostKeyChecking=no "
                   f"-o UserKnownHostsFile=/dev/null")
        a, b = (src, f"{user}@{ip}:{dst}") if upload else (f"{user}@{ip}:{src}", dst)
        r = subprocess.run(["rsync", "-az", "--partial", "--inplace", "-e", ssh_cmd, a, b],
                           capture_output=True, text=True, timeout=self.timeout)
        if r.returncode != 0 and not self.quiet:
            print(f"    rsync 失败，回退 tar: {(r.stderr or '').strip()[:120]}")
        return r.returncode == 0

    # ---------------------------------------------------------------- 工具
    def _classify(self, remote: str):
        if any(ch in remote for ch in "*?["):
            base = posixpath.dirname(remote) or "/"
            pat = posixpath.basename(remote)
            rc, out = self.ssh(f"ls -1 {base} 2>/dev/null", timeout=60)
            items = [x for x in out.splitlines() if x and fnmatch.fnmatch(x, pat)]
            return "files", items, base
        # P2：探测超时 30→60s + 重试一次；仍超时则保守按单文件处理（不再抛异常）
        rc, out = self.ssh(f"if [ -d {remote} ]; then echo DIR; else echo FILE; fi",
                           timeout=60, retries=2)
        if rc == 124:
            print(f"    ⚠ 目录探测超时（已重试），按单文件处理: {remote}")
            return "file", [posixpath.basename(remote)], posixpath.dirname(remote)
        if "DIR" in out:
            parent, name = posixpath.dirname(remote.rstrip("/")), posixpath.basename(remote.rstrip("/"))
            rc, out = self.ssh(f"ls -1 {remote}", timeout=60)
            items = [x for x in out.splitlines() if x]
            # tar 打包整个目录（保留目录名）
            return "dir", [name], parent
        return "file", [posixpath.basename(remote)], posixpath.dirname(remote)

    def _remote_size(self, items: list[str], base: str) -> int:
        if not items:
            return 0
        paths = " ".join(self._q(posixpath.join(base, i) if base else i) for i in items)
        rc, out = self.ssh(f"du -sb {paths} 2>/dev/null | awk '{{s+=$1}} END {{print s}}'", timeout=60)
        try:
            return int((out or "0").strip().splitlines()[0])
        except Exception:
            return 0

    def _same_size(self, remote: str, local_p: Path) -> bool:
        total = self._remote_size([posixpath.basename(remote)], posixpath.dirname(remote))
        try:
            return total == 0 or local_p.stat().st_size == total
        except Exception:
            return False

    def verify(self, remote_paths: list[str]) -> dict:
        """远端 sha256 + 大小清单，用于下载后比对。"""
        paths = " ".join(self._q(p) for p in remote_paths)
        rc, out = self.ssh(f"sha256sum {paths} 2>/dev/null; echo ---; "
                           f"ls -l {paths} 2>/dev/null | awk '{{print $5, $9}}'", timeout=180)
        sums, sizes, sec = {}, {}, 0
        for line in out.splitlines():
            if line.strip() == "---":
                sec = 1
                continue
            parts = line.split()
            if len(parts) >= 2:
                if sec == 0:
                    sums[parts[1]] = parts[0]
                else:
                    try:
                        sizes[parts[1]] = int(parts[0])
                    except ValueError:
                        pass
        return {"sha256": sums, "size": sizes}

    def _should_gzip(self, items: list[str]) -> bool:
        exts = {posixpath.splitext(i)[1].lower() for i in items}
        if not exts:
            return False
        return all(e in TEXT_EXT for e in exts) and not (exts & NO_COMPRESS)

    @staticmethod
    def _q(p: str) -> str:
        return "'" + p.replace("'", "'\\''") + "'"

    @staticmethod
    def _dir_size(p: Path) -> int:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def self_test() -> int:
    """本地无网自检：验证分类/压缩判断/工具函数。"""
    t = Transfer.__new__(Transfer)
    t.quiet = True
    print("TEXT gzip:", t._should_gzip(["a.md", "b.py"]))
    print("IMG  gzip:", t._should_gzip(["a.png", "b.jpg"]))
    print("human:", human(1892239), human(1024 * 1024 * 3.5))
    print("quote:", Transfer._q("/root/a b'c.png"))
    return 0


if __name__ == "__main__":
    sys.exit(self_test())
