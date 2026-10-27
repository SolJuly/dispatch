#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对指定实例执行一组只读/轻量校验命令，输出带分节的结构化结果。

用法: python3 scripts/verify_remote.py <实例ID> [batch1|batch2]
密码/端口都从 API 实时取，不落盘。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from compshare.cs import CS  # noqa: E402


def run_batch(cs: CS, uid: str, script: str, timeout: int = 180) -> str:
    inst = cs.get(uid)
    ip = cs.public_ip(inst)
    pw = cs.password_of(inst)
    login = (cs.ssh_of(inst) or "").split()
    port = "22"
    user = "root"
    for i, tok in enumerate(login):
        if tok == "-p" and i + 1 < len(login):
            port = login[i + 1]
        if "@" in tok:
            user = tok.split("@")[0] or "root"
    cmd = ["sshpass", "-p", pw, "ssh", "-p", port, "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
           f"{user}@{ip}", "bash -s"]
    r = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr.strip() else "")


BATCH1 = r'''
set +e
sec(){ echo; echo "===== $* ====="; }
sec OS
cat /etc/os-release 2>/dev/null | head -4; echo "kernel: $(uname -r)"; echo "arch: $(uname -m)"
sec CPU/MEM
echo "vCPU: $(nproc)"; free -g | sed -n '1,2p'
sec GPU
nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv,noheader
nvidia-smi | sed -n '1,12p'
sec CUDA
command -v nvcc >/dev/null && nvcc --version | tail -2 || echo "nvcc: 不在 PATH"
ls -d /usr/local/cuda* 2>/dev/null | head -5
echo "CUDA driver API: $(nvidia-smi | grep -o 'CUDA Version: [0-9.]*' | head -1)"
sec Python
echo "python3: $(python3 -V 2>&1) at $(command -v python3)"
python3 - <<'PY'
import sys
print("exe:", sys.executable, "ver:", sys.version.split()[0])
try:
    import torch
    print("torch:", torch.__version__, "| built cuda:", torch.version.cuda,
          "| cuda avail:", torch.cuda.is_available(),
          "| dev:", (torch.cuda.get_device_name(0) if torch.cuda.is_available() else "-"))
    if torch.cuda.is_available():
        print("capability:", torch.cuda.get_device_capability(0), "| cudnn:", torch.backends.cudnn.version())
        x = torch.randn(1024, 1024, device="cuda"); y = (x @ x).sum().item()
        print("matmul on gpu ok:", isinstance(y, float))
except Exception as e:
    print("torch ERR:", type(e).__name__, e)
PY
pip list 2>/dev/null | grep -i -E "^(torch|torchvision|torchaudio|numpy|xformers|transformers|comfyui|sageattention|triton) " | head -12
sec DISK
df -hT / /root 2>/dev/null | head -4
echo "--- lsblk ---"; lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT 2>/dev/null | head -10
echo "--- 各挂载点容量 ---"; df -h | awk 'NR==1 || $1!="tmpfs"' | head -8
sec 目录布局
for d in /root /workspace /data /opt /home; do [ -d "$d" ] && echo "$d: $(ls -A $d 2>/dev/null | head -8 | tr '\n' ' ')"; done
sec COMFY 安装位置
for d in /root/ComfyUI /workspace/ComfyUI /opt/ComfyUI /root/comfyui; do [ -d "$d" ] && echo "FOUND $d"; done
find / -maxdepth 4 -type f -name "main.py" -path "*omfy*" 2>/dev/null | head -3
find / -maxdepth 4 -type d -name "custom_nodes" 2>/dev/null | head -3
sec 进程
ps -eo pid,etime,cmd --sort=-%mem 2>/dev/null | grep -i -E "comfy|main\.py|jupyter|filebrowser" | grep -v grep | head -8
sec systemd 服务
systemctl list-units --type=service --no-pager --no-legend 2>/dev/null | grep -i -E "comfy|jupyter|filebrowser|start" | head -8
sec 自启动配置
ls -l /etc/rc.local 2>/dev/null; crontab -l 2>/dev/null | head -5
ls -l /root/*.sh 2>/dev/null | head -5; ls -l /etc/systemd/system/*.service 2>/dev/null | head -8
sec 监听端口
ss -ltnp 2>/dev/null | head -12
'''

BATCH2 = r'''
set +e
sec ComfyUI 进程/端口
curl -s -m 5 -o /dev/null -w "8188 http=%{http_code}\n" http://127.0.0.1:8188/ || echo "8188 无响应"
curl -s -m 5 http://127.0.0.1:8188/system_stats || echo "(system_stats 无响应)"
sec ComfyUI 版本与目录
for d in /root/ComfyUI /workspace/ComfyUI; do
  [ -d "$d" ] || continue
  echo "DIR=$d"; echo "--- git ---"; (cd $d && git log -1 --format="%h %ad %s" --date=short 2>/dev/null)
  echo "--- 版本文件 ---"; cat $d/comfyui_version.py 2>/dev/null | head -3
  echo "--- requirements 头 ---"; head -5 $d/requirements.txt 2>/dev/null
  echo "--- custom_nodes ---"; ls $d/custom_nodes 2>/dev/null | head -20
  echo "--- models ---"; du -sh $d/models/* 2>/dev/null | head -15
  echo "--- 输出目录 ---"; du -sh $d/output 2>/dev/null; ls $d/output 2>/dev/null | head -5
  echo "--- 启动脚本 ---"; ls -l $d/*.sh 2>/dev/null | head -5
done
sec 磁盘大目录 top
du -h -d 1 / 2>/dev/null | sort -h | tail -8
sec GPU 占用
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader
sec python 依赖（comfy 侧）
pip list 2>/dev/null | grep -i -E "comfy|torch|numpy|xformers|insightface|onnx|opencv" | head -12
'''


BATCH3 = r'''
set +e
sec ComfyUI 运行时
pid=$(pgrep -f "main.py --listen" | head -1); echo "pid=$pid"
if [ -n "$pid" ]; then
  echo "exe: $(readlink /proc/$pid/exe)"; echo "cwd: $(readlink /proc/$pid/cwd)"
  echo "cmdline: $(tr '\0' ' ' < /proc/$pid/cmdline)"; echo "started: $(ps -o lstart= -p $pid)"
fi
sec 启动脚本
ls -l /start.d/ 2>/dev/null
echo "--- /start.d/comfyui.sh ---"; cat /start.d/comfyui.sh 2>/dev/null
echo "--- /root/comfyui.sh ---"; cat /root/comfyui.sh 2>/dev/null
echo "--- /root/cleanup.sh ---"; cat /root/cleanup.sh 2>/dev/null
sec ComfyUI 版本
cd /root/ComfyUI 2>/dev/null && { git log -1 --format="%h %ad %s" --date=short 2>/dev/null; cat comfyui_version.py 2>/dev/null | head -3; }
sec 解释器内 torch
py=$(readlink /proc/$pid/exe 2>/dev/null)
if [ -n "$py" ]; then
  $py - <<'PYEOF'
import sys
print("python", sys.version.split()[0])
try:
    import torch
    print("torch", torch.__version__, "cuda", torch.version.cuda, "avail", torch.cuda.is_available())
    if torch.cuda.is_available():
        print("device", torch.cuda.get_device_name(0), "cap", torch.cuda.get_device_capability(0))
        x = torch.randn(2048, 2048, device="cuda"); print("gpu matmul ok:", float((x @ x).sum()))
except Exception as e:
    print("torch ERR", type(e).__name__, e)
PYEOF
fi
sec ComfyUI API
curl -s -m 5 http://127.0.0.1:8188/system_stats | head -c 900; echo
curl -s -m 5 -o /dev/null -w "GET / -> %{http_code}\n" http://127.0.0.1:8188/
curl -s -m 5 http://127.0.0.1:8188/object_info 2>/dev/null | head -c 200; echo
sec 插件 custom_nodes
echo "--- /root/ComfyUI/custom_nodes ---"; ls /root/ComfyUI/custom_nodes 2>/dev/null | head -30
echo "--- /model/comfyui/custom_nodes ---"; ls /model/comfyui/custom_nodes 2>/dev/null | head -20
sec 模型占用
du -sh /root/ComfyUI/models/* 2>/dev/null | sort -h | tail -10
echo "--- /model top ---"; du -sh /model/* 2>/dev/null | sort -h | tail -10
sec 输出目录
ls -la /root/ComfyUI/output 2>/dev/null | head -8
df -h /root/ComfyUI/output 2>/dev/null | tail -1
'''

BATCH4 = r'''
set +e
sec 标记文件与持久化
echo "marker: $(cat /root/persist_marker.txt 2>/dev/null || echo MISSING)"
ls -l /root/persist_marker.txt 2>/dev/null
sec nohup 后台任务存活
echo "sleep300: $(pgrep -f 'sleep 300' | tr '\n' ' ')"
sec 自启动是否已拉起
echo "comfy pid: $(pgrep -f 'main.py --listen' | head -1)"
curl -s -m 5 -o /dev/null -w "8188 -> %{http_code}\n" http://127.0.0.1:8188/
sec 运行时长/开机时间
uptime -p; echo "boot: $(uptime -s)"
sec 磁盘与模型是否还在
df -h / | tail -1; ls /model 2>/dev/null | head -5
'''

def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    uid = sys.argv[1]
    batch = sys.argv[2] if len(sys.argv) > 2 else "batch1"
    script = {"batch1": BATCH1, "batch2": BATCH2, "batch3": BATCH3,
              "batch4": BATCH4}.get(batch)
    if not script:
        print("未知批次，可选 batch1..batch4")
        return 2
    out = run_batch(CS(), uid, script)
    print(out)
    report_dir = Path(__file__).resolve().parent.parent / "reports"
    report_dir.mkdir(exist_ok=True)
    f = report_dir / f"verify-{batch}-{uid}.log"
    f.write_text(out, encoding="utf-8")
    print(f"\n[saved] {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
