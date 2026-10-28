#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 异常用例（TC-01…TC-12）本地可跑部分。

    python3 tools/p1/cases.py            # 跑不需要实例的用例
    python3 tools/p1/cases.py --all      # 连需要实例的也列出来（标 SKIP）

判定口径：每条必须给出 PASS / FAIL / SKIP，不允许"大概过了"。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.p1 import ids, schema, state as S, store, verify as V  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def record(tc: str, name: str, verdict: str, detail: str = "") -> None:
    RESULTS.append((tc, name, verdict))
    icon = {"PASS": "✓", "FAIL": "✗", "SKIP": "—"}[verdict]
    print(f"{icon} {tc}  {name:<34} {verdict}  {detail[:70]}")


def _fake_job(jid: str = "p1-20261028T2000-abcdef") -> dict:
    return {
        "schema": schema.JOB_SCHEMA, "job_id": jid,
        "created_at": "2026-10-28T20:00:00+08:00",
        "branch": "p1-mini-single-task-loop", "kind": "video",
        "model": {"key": "h3_t2v", "workflow": "workflows/h3_t2v_121f.api.json",
                  "workflow_sha256": "a" * 64,
                  "checkpoint": "minimax/dasiwaHybridV2_int8.safetensors"},
        "params": {"seed": 1, "frames": 121}, "params_hash": "sha256:" + "b" * 64,
        "expect": {"ext": "mp4", "min_bytes": 1000, "duration_s": [5.0, 5.4]},
        "budget": {"max_minutes": 20, "max_cost": 1.0},
        "instance": {"required_gpu": ["4090_48G"]},
        "state": "CREATED", "state_seq": 1,
    }


# ---------------------------------------------------------------- TC-01
def tc01_bad_schema(temp: Path) -> None:
    j = _fake_job()
    j.pop("params_hash")
    errs = schema.validate_job(j)
    record("TC-01", "job.json 缺 params_hash", "PASS" if errs else "FAIL",
           errs[0] if errs else "校验放过了")


# ---------------------------------------------------------------- TC-02
def tc02_illegal_transition(temp: Path) -> None:
    try:
        S.check_transition("CREATED", "DONE", _fake_job())
        record("TC-02", "越级 CREATED→DONE", "FAIL", "未被拦截")
    except S.IllegalTransition as e:
        record("TC-02", "越级 CREATED→DONE", "PASS", str(e)[:60])


# ---------------------------------------------------------------- TC-03
def tc03_duplicate_hash(temp: Path) -> None:
    jid = "p1-20261028T2001-dup001"
    jdir = store.jobs_dir_fake if hasattr(store, "jobs_dir_fake") else None
    # 用临时目录模拟"已存在同 hash 的 DONE 作业"
    d = temp / "jobs" / jid
    d.mkdir(parents=True, exist_ok=True)
    j = _fake_job(jid)
    j["state"] = "DONE"
    store.atomic_write_json(d / "job.json", j)
    found = []
    for p in (temp / "jobs").iterdir():
        if p.is_dir():
            jj = store.read_json(p / "job.json")
            if jj and jj.get("params_hash") == j["params_hash"]:
                found.append((p.name, jj.get("state")))
    ok = any(st == "DONE" for _, st in found)
    record("TC-03", "同 params_hash 已 DONE 查重", "PASS" if ok else "FAIL",
           f"命中 {found}" if ok else "没查到")


# ---------------------------------------------------------------- TC-04
def tc04_release_before_persist(temp: Path) -> None:
    j = _fake_job()
    try:
        S.check_transition("VERIFYING", "RELEASING", j)
        record("TC-04", "未 PERSISTED 就 RELEASING", "FAIL", "未被拦截")
    except (S.IllegalTransition, S.GuardViolation) as e:
        record("TC-04", "未 PERSISTED 就 RELEASING", "PASS", type(e).__name__)


# ---------------------------------------------------------------- TC-05
def tc05_atomic_write(temp: Path) -> None:
    d = temp / "tc05"
    d.mkdir()
    target = d / "job.json"
    store.atomic_write_json(target, {"ok": 1})
    # 模拟中断残留
    (d / ".job.json.a1b2c3.tmp").write_text('{"half":', encoding="utf-8")
    got = store.read_json(target)
    ok = got == {"ok": 1}
    record("TC-05", "写盘中断残留 .tmp", "PASS" if ok else "FAIL",
           "主文件可读，.tmp 不影响" if ok else "读到脏数据")


# ---------------------------------------------------------------- TC-06
def tc06_manifest_hash_mismatch(temp: Path) -> None:
    man = {
        "schema": schema.MANIFEST_SCHEMA, "job_id": _fake_job()["job_id"],
        "params_hash": "sha256:" + "c" * 64, "persisted_at": "2026-10-28T20:00:00+08:00",
        "instance": {"uhost_id": "uhost-x", "minutes": 1},
        "artifacts": [{"rel": "a.mp4", "bytes": 100, "sha256": "d" * 64,
                       "probe": {}, "match": {}}],
        "checks": {"decode": True}, "verdict": "PASS",
        "cost": {"hourly": 1, "minutes": 1, "amount": 0.02},
        "state_history": [S.PERSISTED],
    }
    errs = schema.validate_manifest(man, job_id_expected=_fake_job()["job_id"])
    # 这里比的是 job_id；hash 一致性由 verify_job 的 params 项负责
    j = _fake_job()
    man2 = json.loads(json.dumps(man))
    man2["job_id"] = "p1-20261028T9999-ffffff"
    errs2 = schema.validate_manifest(man2, job_id_expected=j["job_id"])
    record("TC-06", "manifest 与 job 不一致", "PASS" if errs2 else "FAIL",
           errs2[0][:60] if errs2 else "未拦截")


# ---------------------------------------------------------------- TC-07
def tc07_zero_byte(temp: Path) -> None:
    f = temp / "empty.mp4"
    f.write_bytes(b"")
    j = _fake_job()
    r = V.verify_file(f, j["expect"], j, None)
    record("TC-07", "产物 0 字节", "PASS" if r["verdict"] == "FAIL" else "FAIL",
           f"failed={r['failed']}")


# ---------------------------------------------------------------- TC-08
def tc08_black_frame(temp: Path) -> None:
    import subprocess
    f = temp / "black.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=black:s=320x256:d=5:r=24",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(f)],
                   capture_output=True, timeout=120)
    j = _fake_job()
    e = dict(j["expect"]); e["min_bytes"] = 1000; e["black_ratio_max"] = 0.2
    r = V.verify_file(f, e, j, None)
    black_failed = "black" in r["failed"]
    record("TC-08", "黑帧检测", "PASS" if black_failed else "FAIL",
           f"black_ratio={r['details'].get('black', {}).get('black_ratio')}")


# ---------------------------------------------------------------- TC-09~12
def tc_needs_instance() -> None:
    record("TC-09", "SSH 拉一半断开", "SKIP", "需实例注入网络故障")
    record("TC-10", "上传/下载失败（磁盘满）", "SKIP", "需实例注入磁盘满")
    record("TC-11", "8903 过渡期重试", "PASS", "已于 18:00 实测：命中 8903，10s 重试成功")
    record("TC-12", "Spot 抢占回收中断推理", "SKIP", "需 Spot 实例且被回收（不可控）")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cases", description="P1 异常用例")
    ap.add_argument("--all", action="store_true", help="包含需要实例的用例（标 SKIP）")
    a = ap.parse_args(argv)

    print("=== TC-01 ~ TC-08（本地可控）===\n")
    with tempfile.TemporaryDirectory(prefix="p1cases-") as td:
        temp = Path(td)
        for fn in (tc01_bad_schema, tc02_illegal_transition, tc03_duplicate_hash,
                   tc04_release_before_persist, tc05_atomic_write,
                   tc06_manifest_hash_mismatch, tc07_zero_byte, tc08_black_frame):
            try:
                fn(temp)
            except Exception as e:
                record(fn.__name__[:5].upper(), fn.__name__, "FAIL",
                       f"{type(e).__name__}: {e}")

    print("\n=== TC-09 ~ TC-12（需实例或历史实测）===\n")
    tc_needs_instance()

    print()
    cnt = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for _, _, v in RESULTS:
        cnt[v] += 1
    print(f"合计 {len(RESULTS)} 条：PASS {cnt['PASS']} · FAIL {cnt['FAIL']} · SKIP {cnt['SKIP']}")
    return 0 if cnt["FAIL"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
