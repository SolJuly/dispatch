#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2.0 登记库：把 registry/ 下的 JSON 配置导入 SQLite，并提供解析/兼容性查询。

职责边界（刻意做小）：
  - 只负责「配置 → 索引」和「按 key 取配置 + 兼容性判定」，不执行 ComfyUI、不建机。
  - 兼容性不是猜的：verified 必须带真实成功作业证据；结构+模型文件完全一致的
    工作流可继承已验证结论（equivalent）；其余一律 experimental，默认拒绝执行。
  - DB 是派生索引：删掉 registry.db 后 `p20ctl import` 可完整重建；
    唯一事实来源是 registry/*.json + registry/compat.json。

表：
  model / model_version            模型配置与版本快照
  workflow / workflow_version      工作流配置、图快照、结构指纹
  workflow_model                   兼容性矩阵（verified/equivalent/experimental/incompatible）
  registry_event                   导入/变更审计
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from . import config_schema as cs

ROOT = Path(__file__).resolve().parents[2]
REG_DIR = ROOT / "registry"
WORKFLOW_DIR = REG_DIR / "workflows"
MODEL_DIR = REG_DIR / "models"
COMPAT_JSON = REG_DIR / "compat.json"
DB_PATH = REG_DIR / "registry.db"

SCHEMA_VERSION = 1
ACTIVE_STATUS = ("verified", "equivalent")

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS model (
  key            TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  kind           TEXT NOT NULL,
  family         TEXT,
  version        INTEGER NOT NULL,
  primary_role   TEXT,
  source_path    TEXT,
  source_sha256  TEXT,
  config_json    TEXT NOT NULL,
  imported_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_version (
  model_key      TEXT NOT NULL,
  version        INTEGER NOT NULL,
  source_sha256  TEXT,
  config_json    TEXT NOT NULL,
  imported_at    TEXT NOT NULL,
  PRIMARY KEY (model_key, version)
);

CREATE TABLE IF NOT EXISTS workflow (
  key            TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  kind           TEXT NOT NULL,
  engine         TEXT NOT NULL,
  version        INTEGER NOT NULL,
  model_key      TEXT NOT NULL,
  graph_path     TEXT NOT NULL,
  graph_sha256   TEXT NOT NULL,
  structure_sig  TEXT NOT NULL,
  model_sig      TEXT NOT NULL,
  placeholders   INTEGER NOT NULL DEFAULT 0,
  source_path    TEXT,
  source_sha256  TEXT,
  config_json    TEXT NOT NULL,
  graph_json     TEXT NOT NULL,
  imported_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_version (
  workflow_key   TEXT NOT NULL,
  version        INTEGER NOT NULL,
  source_sha256  TEXT,
  graph_sha256   TEXT,
  config_json    TEXT NOT NULL,
  graph_json     TEXT NOT NULL,
  imported_at    TEXT NOT NULL,
  PRIMARY KEY (workflow_key, version)
);

CREATE TABLE IF NOT EXISTS workflow_model (
  workflow_key   TEXT NOT NULL,
  model_key      TEXT NOT NULL,
  status         TEXT NOT NULL,
  basis          TEXT,
  evidence_json  TEXT,
  checked_at     TEXT,
  notes          TEXT,
  PRIMARY KEY (workflow_key, model_key)
);

CREATE TABLE IF NOT EXISTS registry_event (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       INTEGER NOT NULL,
  action   TEXT NOT NULL,
  target   TEXT,
  detail   TEXT
);

CREATE INDEX IF NOT EXISTS ix_wf_model ON workflow(model_key, kind);
CREATE INDEX IF NOT EXISTS ix_wf_structure ON workflow(structure_sig);
CREATE INDEX IF NOT EXISTS ix_wm_status ON workflow_model(status);
CREATE INDEX IF NOT EXISTS ix_event_ts ON registry_event(ts);
"""


class RegistryError(Exception):
    """登记库错误（配置错误 / 找不到 key / 兼容性拒绝）。"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _sha_text(s: str) -> str:
    return cs.sha256_obj(s)


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(DDL)
    return c


def _json(v) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(v, default=None):
    try:
        return json.loads(v) if v else default
    except (TypeError, json.JSONDecodeError):
        return default


# ------------------------------------------------------------------ 指纹
def source_files(workflow_dir: Path = WORKFLOW_DIR, model_dir: Path = MODEL_DIR,
                 compat_path: Path = COMPAT_JSON) -> list[Path]:
    files = sorted(workflow_dir.glob("*.json")) + sorted(model_dir.glob("*.json"))
    if compat_path.exists():
        files.append(compat_path)
    # 加上每份配置引用的图文件，图被改动也能触发重建
    for p in sorted(workflow_dir.glob("*.json")):
        try:
            d = cs.load_json(p)
        except cs.ConfigError:
            continue
        g = d.get("graph")
        if isinstance(g, str) and g:
            gp = Path(g)
            if not gp.is_absolute():
                gp = ROOT / g
            if gp.exists():
                files.append(gp)
    return files


def _rel(p: Path) -> str:
    """仓库内给相对路径，仓库外（测试临时目录）给绝对路径——不能因此吞掉 sha。"""
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return str(Path(p).resolve())


def source_fingerprint(workflow_dir: Path = WORKFLOW_DIR, model_dir: Path = MODEL_DIR,
                       compat_path: Path = COMPAT_JSON) -> str:
    items = []
    for p in sorted({str(x.resolve()) for x in source_files(workflow_dir, model_dir, compat_path)}):
        try:
            items.append([_rel(Path(p)), cs.sha256_file(Path(p))])
        except OSError:
            items.append([_rel(Path(p)), ""])
    return cs.sha256_obj(items)


def _meta(c, k: str) -> str | None:
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


def _set_meta(c, k: str, v: str) -> None:
    c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))


# ------------------------------------------------------------------ 导入
def _discover(workflow_dir: Path, model_dir: Path) -> tuple[dict, dict]:
    models, workflows = {}, {}
    errors: list[str] = []
    for p in sorted(model_dir.glob("*.json")):
        try:
            d = cs.load_and_validate("model", p)
            models[d["key"]] = (d, p)
        except cs.ConfigError as e:
            errors += [f"{p.name}: {x}" for x in e.errors]
    for p in sorted(workflow_dir.glob("*.json")):
        try:
            d = cs.load_and_validate("workflow", p, root=ROOT)
            workflows[d["key"]] = (d, p)
        except cs.ConfigError as e:
            errors += [f"{p.name}: {x}" for x in e.errors]
    if errors:
        raise cs.ConfigError(errors, "registry/")
    return models, workflows


def import_all(db_path: Path | str = DB_PATH, *, workflow_dir: Path = WORKFLOW_DIR,
               model_dir: Path = MODEL_DIR, compat_path: Path = COMPAT_JSON,
               quiet: bool = False) -> dict:
    """全量重建派生索引（幂等）。配置有错 → 整体失败，不写半成品。"""
    models, workflows = _discover(workflow_dir, model_dir)

    # ---- 接口完整性门禁（2026-11-11 起强制）----
    #
    # 你的要求：「每次新添工作流时把这个固定成流程，UI 必须严格按工作流给的
    # 模板，输入一个都不能少。」
    #
    # 所以它是**写库之前的硬门禁**，不是事后提示：
    # 图里有用户可给的输入（图片/视频/音频/提示词）却没在 registry.params
    # 里暴露，直接拒绝导入。
    #
    # 为什么必须硬卡 —— 这种缺陷在别的环节全都看不见：
    #   · 预检只查节点类和权重，全绿
    #   · 渲染能过（没暴露的参数不参与渲染）
    #   · 真机能跑，只是"少了个输入框"，用户以为这个功能不存在
    # 实测代价：minimax H3 的 ref_audios 支持 3 段参考音频，图里位置一直在，
    # 但没暴露参数 → 谁都不知道有这个能力。
    #
    # 必须在拿 DB 连接**之前**做：门禁要读现有 registry 算"已暴露"，
    # 而事务里读到的是写了一半的状态。
    _gate = _port_gaps(preview_workflows=workflows)
    if _gate:
        detail = "；".join(f"{k} 缺 {v} 处" for k, v in list(_gate.items())[:8])
        more = f" 等 {len(_gate)} 条" if len(_gate) > 8 else ""
        raise cs.ConfigError(
            [f"接口完整性门禁失败{more}：{detail}",
             "每个『用户可给』的输入都必须在 registry.params 里暴露 —— "
             "否则 UI 少一个上传框，用户会以为这个功能不存在。",
             "查看详情：python3 tools/p20/workflow_ports.py --gaps",
             "确认是误报时：临时设 P21_SKIP_PORT_GATE=1（不建议长期用）"],
            str(workflow_dir))

    # 兼容性文件（可缺省 = 全部 experimental）
    compat_entries: list[dict] = []
    if compat_path.exists():
        d = cs.load_json(compat_path)
        errs = cs.validate_compat(d)
        if errs:
            raise cs.ConfigError(errs, str(compat_path))
        compat_entries = d.get("entries") or []
        for en in compat_entries:
            if en["workflow"] not in workflows:
                raise cs.ConfigError([f"compat 引用了不存在的工作流：{en['workflow']}"], str(compat_path))
            if en["model"] not in models:
                raise cs.ConfigError([f"compat 引用了不存在的模型：{en['model']}"], str(compat_path))

    # 工作流 → 模型槽位必须被模型文件填满
    for key, (wf, path) in workflows.items():
        mk = wf["model"]["key"]
        if mk not in models:
            raise cs.ConfigError([f"model.key={mk!r} 没有对应模型配置"], str(path))
        mfiles = set(models[mk][0].get("files") or {})
        missing = set((wf.get("model") or {}).get("slots") or {}) - mfiles
        if missing:
            raise cs.ConfigError([f"模型 {mk} 缺少槽位文件：{sorted(missing)}"], str(path))

    fp = source_fingerprint(workflow_dir, model_dir, compat_path)
    db_path = Path(db_path)
    c = connect(db_path)
    try:
        c.execute("BEGIN IMMEDIATE")
        for t in ("model", "model_version", "workflow", "workflow_version", "workflow_model"):
            c.execute(f"DELETE FROM {t}")
        now = _now()
        for key, (d, p) in models.items():
            cfg = _json(d)
            c.execute("INSERT INTO model(key,name,kind,family,version,primary_role,source_path,"
                      "source_sha256,config_json,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (key, d["name"], d["kind"], d.get("family"), d["version"],
                       d.get("primary") or ("ckpt" if "ckpt" in (d.get("files") or {}) else "unet"),
                       _rel(p), cs.sha256_file(p), cfg, now))
            c.execute("INSERT INTO model_version(model_key,version,source_sha256,config_json,imported_at) "
                      "VALUES(?,?,?,?,?)", (key, d["version"], cs.sha256_file(p), cfg, now))
        for key, (d, p) in workflows.items():
            gpath = Path(d["graph"])
            if not gpath.is_absolute():
                gpath = ROOT / gpath
            graph = cs.load_graph(gpath)
            gsha = cs.sha256_file(gpath)
            struct = cs.structure_signature(graph)
            model_sig = cs.model_signature(models[d["model"]["key"]][0])
            cfg = _json(d)
            gj = _json(graph)
            c.execute("INSERT INTO workflow(key,name,kind,engine,version,model_key,graph_path,"
                      "graph_sha256,structure_sig,model_sig,placeholders,source_path,source_sha256,"
                      "config_json,graph_json,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (key, d["name"], d["kind"], d["engine"], d["version"], d["model"]["key"],
                       d["graph"], gsha, struct, model_sig, len(cs.placeholder_sites(graph)),
                       _rel(p), cs.sha256_file(p), cfg, gj, now))
            c.execute("INSERT INTO workflow_version(workflow_key,version,source_sha256,graph_sha256,"
                      "config_json,graph_json,imported_at) VALUES(?,?,?,?,?,?,?)",
                      (key, d["version"], cs.sha256_file(p), gsha, cfg, gj, now))

        # 1) 显式结论
        explicit = {(en["workflow"], en["model"]): en for en in compat_entries}
        for (wk, mk), en in explicit.items():
            c.execute("INSERT OR REPLACE INTO workflow_model(workflow_key,model_key,status,basis,"
                      "evidence_json,checked_at,notes) VALUES(?,?,?,?,?,?,?)",
                      (wk, mk, en["status"], en.get("basis", "manual"), _json(en.get("evidence") or []),
                       en.get("verified_at") or now, en.get("notes", "")))
        # 2) 结构/模型完全一致 → 继承 verified（equivalent）
        verified = [(r["key"], r["model_key"], r["structure_sig"], r["model_sig"])
                    for r in c.execute("SELECT key,model_key,structure_sig,model_sig FROM workflow "
                                       "ORDER BY key").fetchall()
                    if (r["key"], r["model_key"]) in explicit
                    and explicit[(r["key"], r["model_key"])]["status"] == "verified"]
        rows = c.execute("SELECT key,model_key,structure_sig,model_sig FROM workflow ORDER BY key").fetchall()
        for r in rows:
            wk, mk = r["key"], r["model_key"]
            if (wk, mk) in explicit:
                continue
            src = next((v for v in verified
                        if v[1] == mk and v[2] == r["structure_sig"] and v[3] == r["model_sig"]), None)
            if src:
                ev = explicit[(src[0], src[1])].get("evidence") or []
                c.execute("INSERT OR REPLACE INTO workflow_model(workflow_key,model_key,status,basis,"
                          "evidence_json,checked_at,notes) VALUES(?,?,?,?,?,?,?)",
                          (wk, mk, "equivalent", f"structure-equal:{src[0]}", _json(ev),
                           now, "同一模型文件 + 同一图结构，继承已验证结论"))
            else:
                c.execute("INSERT OR REPLACE INTO workflow_model(workflow_key,model_key,status,basis,"
                          "evidence_json,checked_at,notes) VALUES(?,?,?,?,?,?,?)",
                          (wk, mk, "experimental", "unverified", "[]", now,
                           "没有真实成功作业证据；执行需显式 --allow-experimental"))
        _set_meta(c, "schema_version", str(SCHEMA_VERSION))
        _set_meta(c, "fingerprint", fp)
        _set_meta(c, "imported_at", now)
        c.execute("INSERT INTO registry_event(ts,action,target,detail) VALUES(?,?,?,?)",
                  (int(time.time()), "import", "registry",
                   f"{len(workflows)} workflows / {len(models)} models / "
                   f"{len(explicit)} explicit compat"))
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        c.close()
    if not quiet:
        print(f"✓ registry 已重建：{len(workflows)} 个工作流 · {len(models)} 个模型 · "
              f"显式兼容 {len(compat_entries)} 条 → {db_path}")
    return {"workflows": len(workflows), "models": len(models),
            "compat_explicit": len(compat_entries), "fingerprint": fp, "db": str(db_path)}


def _port_gaps(preview_workflows: list[dict] | None = None) -> dict[str, int]:
    """有多少工作流存在未暴露的用户输入。返回 {workflow: 缺口数}。

    `preview_workflows` 是**尚未落库**的工作流配置（import 时传进来）——
    门禁必须在写库之前判断，所以不能走 `list_workflows()` 读 DB。
    """
    import os as _os
    if _os.environ.get("P21_SKIP_PORT_GATE") == "1":
        return {}
    try:
        from tools.p20 import workflow_ports as wp
        oi = wp.load_object_info()
    except Exception as e:                 # noqa: BLE001
        # **不能静默返回空**（2026-11-11 踩到）：门禁代码自己写错时，
        # `return {}` 会让它看起来"全部通过"，于是永远拦不住任何东西。
        # 这种情况必须喊出来 —— 门禁坏了和门禁通过是两回事。
        import warnings
        warnings.warn(f"接口门禁不可用（{type(e).__name__}: {e}），本次跳过检查",
                      RuntimeWarning, stacklevel=2)
        return {}

    # `_discover()` 返回的是 {key: (wf_dict, path)}，不是 list ——
    # 之前按 list 遍历，拿到的是 key 字符串，`wf.get()` 静默失败，
    # 门禁永远返回空（2026-11-11 实测踩到，拦不住任何东西）。
    out: dict[str, int] = {}
    items = (preview_workflows or {}).items() if isinstance(preview_workflows, dict) \
        else [(None, w) for w in (preview_workflows or [])]
    for key, item in items:
        wf = item[0] if isinstance(item, tuple) else item
        if not isinstance(wf, dict):
            continue
        try:
            r = wp.scan_one(wf, oi)
        except Exception as e:             # noqa: BLE001
            import warnings
            warnings.warn(f"接口门禁扫描 {key} 失败（{type(e).__name__}: {e}）",
                          RuntimeWarning, stacklevel=2)
            continue
        if not r:
            continue
        g = r.gaps()
        n = sum(len(v) for v in g.values())
        if n:
            out[str(key or wf.get("key") or "?")] = n
    return out

def ensure_fresh(db_path: Path | str = DB_PATH, *, quiet: bool = True) -> bool:
    """DB 不存在或源文件指纹变化 → 自动重建。返回是否发生了重建。

    只对默认库做自动重建；测试/临时库（传入其它 db_path）由调用方显式 import，
    避免「查临时库时被默认配置覆盖」这种隐蔽事故。
    """
    db_path = Path(db_path)
    if db_path.resolve() != DB_PATH.resolve():
        return False
    fp = source_fingerprint()
    if db_path.exists():
        try:
            c = connect(db_path)
            try:
                if _meta(c, "fingerprint") == fp:
                    return False
            finally:
                c.close()
        except sqlite3.Error:
            pass
    import_all(db_path, quiet=quiet)
    return True


# ------------------------------------------------------------------ 查询
def _model_view(row) -> dict:
    d = _loads(row["config_json"], {}) if "config_json" in row.keys() else {}
    out = dict(d) if isinstance(d, dict) else {}
    out.update({"version": row["version"], "source_path": row["source_path"] if "source_path" in row.keys() else None,
                "imported_at": row["imported_at"] if "imported_at" in row.keys() else None})
    return out


def _wf_view(row) -> dict:
    d = _loads(row["config_json"], {})
    out = dict(d) if isinstance(d, dict) else {}
    extra = {"version": row["version"], "graph_sha256": row["graph_sha256"],
             "structure_sig": row["structure_sig"], "model_sig": row["model_sig"],
             "source_path": row["source_path"], "imported_at": row["imported_at"]}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def get_workflow(key: str, *, version: int | None = None, db_path: Path = DB_PATH) -> dict:
    ensure_fresh(db_path)
    c = connect(db_path)
    try:
        if version is not None:
            r = c.execute("SELECT * FROM workflow_version WHERE workflow_key=? AND version=?",
                          (key, int(version))).fetchone()
            if not r:
                raise RegistryError(f"工作流 {key} 没有版本 {version}")
            out = _loads(r["config_json"], {})
            out.update({"version": r["version"], "graph_sha256": r["graph_sha256"],
                        "source_sha256": r["source_sha256"], "snapshot": True})
            out["graph_obj"] = _loads(r["graph_json"], {})
            return out
        r = c.execute("SELECT * FROM workflow WHERE key=?", (key,)).fetchone()
        if not r:
            raise RegistryError(f"工作流不存在：{key}（先 p20ctl import）")
        out = _wf_view(r)
        out["graph_obj"] = _loads(r["graph_json"], {})
        return out
    finally:
        c.close()


def get_model(key: str, *, version: int | None = None, db_path: Path = DB_PATH) -> dict:
    ensure_fresh(db_path)
    c = connect(db_path)
    try:
        if version is not None:
            r = c.execute("SELECT * FROM model_version WHERE model_key=? AND version=?",
                          (key, int(version))).fetchone()
            if not r:
                raise RegistryError(f"模型 {key} 没有版本 {version}")
            out = _loads(r["config_json"], {})
            out.update({"version": r["version"], "snapshot": True,
                        "source_sha256": r["source_sha256"]})
            return out
        r = c.execute("SELECT * FROM model WHERE key=?", (key,)).fetchone()
        if not r:
            raise RegistryError(f"模型不存在：{key}（先 p20ctl import）")
        return _model_view(r)
    finally:
        c.close()


def list_workflows(db_path: Path = DB_PATH) -> list[dict]:
    ensure_fresh(db_path)
    c = connect(db_path)
    try:
        out = []
        for r in c.execute("SELECT * FROM workflow ORDER BY key").fetchall():
            w = _wf_view(r)
            row = c.execute("SELECT status,basis,evidence_json,checked_at FROM workflow_model "
                            "WHERE workflow_key=? AND model_key=?", (r["key"], r["model_key"])).fetchone()
            w["model_key"] = r["model_key"]
            # checked_at 必须带上：UI 要按"最近 N 天验证过"打 NEW 标签。
            # 不带就只能全员无名，或者退回用 imported_at —— 那是派生索引的重建时间，
            # 每跑一次 import 就变，会把全部工作流都标成新的。
            w["compat"] = ({"status": row["status"], "basis": row["basis"],
                            "evidence": _loads(row["evidence_json"], []),
                            "checked_at": row["checked_at"]} if row else
                           {"status": "experimental", "basis": "missing",
                            "evidence": [], "checked_at": None})
            out.append(w)
        return out
    finally:
        c.close()


def list_models(db_path: Path = DB_PATH) -> list[dict]:
    ensure_fresh(db_path)
    c = connect(db_path)
    try:
        return [_model_view(r) for r in c.execute("SELECT * FROM model ORDER BY key").fetchall()]
    finally:
        c.close()


def compat_matrix(db_path: Path = DB_PATH) -> list[dict]:
    ensure_fresh(db_path)
    c = connect(db_path)
    try:
        rows = c.execute("SELECT * FROM workflow_model ORDER BY workflow_key, model_key").fetchall()
        return [{"workflow": r["workflow_key"], "model": r["model_key"], "status": r["status"],
                 "basis": r["basis"], "evidence": _loads(r["evidence_json"], []),
                 "checked_at": r["checked_at"], "notes": r["notes"]} for r in rows]
    finally:
        c.close()


def compat_for(workflow_key: str, model_key: str, db_path: Path = DB_PATH) -> dict:
    ensure_fresh(db_path)
    c = connect(db_path)
    try:
        r = c.execute("SELECT * FROM workflow_model WHERE workflow_key=? AND model_key=?",
                      (workflow_key, model_key)).fetchone()
        if not r:
            return {"workflow": workflow_key, "model": model_key, "status": "experimental",
                    "basis": "missing", "evidence": [], "notes": "没有兼容性记录"}
        return {"workflow": r["workflow_key"], "model": r["model_key"], "status": r["status"],
                "basis": r["basis"], "evidence": _loads(r["evidence_json"], []),
                "checked_at": r["checked_at"], "notes": r["notes"]}
    finally:
        c.close()


def stats(db_path: Path = DB_PATH) -> dict:
    ensure_fresh(db_path)
    c = connect(db_path)
    try:
        by_status = {r["status"]: r["n"] for r in c.execute(
            "SELECT status,COUNT(*) n FROM workflow_model GROUP BY status").fetchall()}
        return {
            "workflows": c.execute("SELECT COUNT(*) n FROM workflow").fetchone()["n"],
            "models": c.execute("SELECT COUNT(*) n FROM model").fetchone()["n"],
            "compat": by_status,
            "fingerprint": _meta(c, "fingerprint"),
            "imported_at": _meta(c, "imported_at"),
            "schema_version": _meta(c, "schema_version"),
        }
    finally:
        c.close()


# ------------------------------------------------------------------ 参数解析
def _validate_value(name: str, spec: dict, value, *, model: dict | None = None,
                    model_key: str = "") -> list[str]:
    e: list[str] = []
    typ = spec.get("type", "string")
    try:
        cs._check_type(name, value, typ, e, "params")
    except Exception:
        pass
    if e:
        return e
    # 枚举取值域：工作流配置里统一用 `values`（config_schema 与前端都按 values 读），
    # 早期只认 `enum` 导致 25 个 enum 参数的校验完全失效——
    # 传 sampler="NO_SUCH_SAMPLER" 会原样写进图送 ComfyUI（2026-11-08 审计实测）。
    allowed = spec.get("values") or spec.get("enum") or []
    if allowed and value not in allowed:
        e.append(f"params.{name}={value!r} 不在枚举 {allowed} 内")
    if typ in ("int", "float"):
        lo = spec.get("min")
        hi = spec.get("max")
        con = (((model or {}).get("constraints") or {}).get(name) or {})
        lo = lo if lo is not None else con.get("min")
        hi = hi if hi is not None else con.get("max")
        if lo is not None and value < lo:
            e.append(f"params.{name}={value} < 下限 {lo}")
        if hi is not None and value > hi:
            e.append(f"params.{name}={value} > 上限 {hi}")
        mult = spec.get("multiple") if spec.get("multiple") is not None else con.get("multiple")
        if mult and value % mult != 0:
            # 帧数惯例是 4n+1（Wan/LTX 的 latent 长度），默认值 81/77 本来就带 +1；
            # 只按 "能被 4 整除" 校验会把模板自己的默认值判成非法（3 个工作流踩中）。
            if not (name == "frames" and mult > 1 and (value - 1) % mult == 0):
                e.append(f"params.{name}={value} 不是 {mult} 的倍数（帧数需满足 {mult}n+1）")
    if typ == "image" and isinstance(value, str) and ("{{" in value or "\n" in value):
        e.append(f"params.{name} 不是合法图片文件名")
    return e


def resolve(workflow_key: str, *, model_key: str | None = None, params: dict | None = None,
            allow_experimental: bool = False, db_path: Path = DB_PATH,
            workflow_version: int | None = None, model_version: int | None = None) -> dict:
    """解析一次作业：取配置 → 查兼容性 → 套默认值 → 校验参数。不渲染、不执行。

    workflow_version/model_version 用于复跑老作业：按版本快照解析，避免配置升级后
    用新 schema 校验旧参数。
    """
    wf = get_workflow(workflow_key, version=workflow_version, db_path=db_path)
    mk = model_key or (wf.get("model") or {}).get("key")
    if not mk:
        raise RegistryError(f"工作流 {workflow_key} 未配置模型")
    model = get_model(mk, version=model_version, db_path=db_path)
    compat = compat_for(workflow_key, mk, db_path=db_path)
    if compat["status"] == "incompatible":
        raise RegistryError(f"兼容性拒绝：{workflow_key} × {mk}（{compat.get('notes') or '标记为不兼容'}）")
    if compat["status"] not in ACTIVE_STATUS and not allow_experimental:
        raise RegistryError(
            f"兼容性未验证：{workflow_key} × {mk}（status={compat['status']}，"
            f"basis={compat['basis']}）。请先真机跑出证据并 p20ctl compat verify，"
            f"或显式加 --allow-experimental。")

    slots = (wf.get("model") or {}).get("slots") or {}
    mfiles = model.get("files") or {}
    missing = [r for r in slots if r not in mfiles]
    if missing:
        raise RegistryError(f"模型 {mk} 缺少工作流需要的槽位文件：{missing}")

    schema = wf.get("params") or {}
    given = dict(params or {})
    resolved, applied, errors = {}, [], []
    for name, spec in schema.items():
        if name in given:
            val = given[name]
        elif "default" in spec:
            val = spec["default"]
            applied.append(name)
        elif name in (model.get("defaults") or {}):
            val = model["defaults"][name]
            applied.append(name)
        elif spec.get("required"):
            errors.append(f"params.{name} 必填但未提供")
            continue
        else:
            continue
        errors += _validate_value(name, spec, val, model=model, model_key=mk)
        resolved[name] = val
    for name in given:
        if name not in schema:
            errors.append(f"params.{name} 不在工作流 {workflow_key} 的参数契约里")
    if errors:
        raise RegistryError("参数校验失败：" + "；".join(errors))
    return {"workflow": wf, "model": model, "compat": compat, "params": resolved,
            "model_key": mk, "defaults_applied": applied, "provided": given}


# ------------------------------------------------------------------ 兼容性写入
def _atomic_write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def set_compat(workflow_key: str, model_key: str, status: str, *, evidence: list[str] | None = None,
               notes: str = "", compat_path: Path = COMPAT_JSON, db_path: Path = DB_PATH) -> dict:
    """写 compat.json（唯一事实来源）并立即重建索引。status 只允许 verified/incompatible。"""
    if status not in ("verified", "incompatible"):
        raise RegistryError("status 只允许 verified / incompatible")
    d = (cs.load_json(compat_path) if compat_path.exists()
         else {"schema": cs.COMPAT_SCHEMA, "entries": []})
    entries = [x for x in (d.get("entries") or [])
               if not (x.get("workflow") == workflow_key and x.get("model") == model_key)]
    entries.append({"workflow": workflow_key, "model": model_key, "status": status,
                    "basis": "manual", "evidence": list(evidence or []),
                    "verified_at": time.strftime("%Y-%m-%d"), "notes": notes})
    d["entries"] = sorted(entries, key=lambda x: (x.get("workflow", ""), x.get("model", "")))
    errs = cs.validate_compat(d)
    if errs:
        raise cs.ConfigError(errs, str(compat_path))
    _atomic_write(compat_path, d)
    import_all(db_path, compat_path=compat_path, quiet=True)
    return compat_for(workflow_key, model_key, db_path=db_path)


def validate_all(db_path: Path = DB_PATH) -> dict:
    """全量体检：配置契约 + 图绑定 + 模型槽位 + 兼容性覆盖。返回结构化结果。"""
    out = {"ok": False, "errors": [], "warnings": [], "counts": {}, "compat": {}}
    try:
        models, workflows = _discover(WORKFLOW_DIR, MODEL_DIR)
    except cs.ConfigError as e:
        out["errors"] += e.errors
        return out
    for key, (wf, p) in workflows.items():
        mk = (wf.get("model") or {}).get("key")
        if mk not in models:
            out["errors"].append(f"{p.name}: model.key={mk} 不存在")
            continue
        missing = set((wf.get("model") or {}).get("slots") or {}) - set(models[mk][0].get("files") or {})
        if missing:
            out["errors"].append(f"{p.name}: 模型 {mk} 缺槽位 {sorted(missing)}")
    try:
        import_all(db_path, quiet=True)
    except (cs.ConfigError, RegistryError) as e:
        out["errors"] += [str(e)]
        return out
    mat = compat_matrix(db_path)
    out["compat"] = {f"{r['workflow']}×{r['model']}": r["status"] for r in mat}
    for r in mat:
        if r["status"] == "experimental":
            out["warnings"].append(
                f"{r['workflow']} × {r['model']} 未验证（执行需 --allow-experimental）")
    out["counts"] = {"workflows": len(workflows), "models": len(models),
                     "verified": sum(1 for r in mat if r["status"] == "verified"),
                     "equivalent": sum(1 for r in mat if r["status"] == "equivalent"),
                     "experimental": sum(1 for r in mat if r["status"] == "experimental"),
                     "incompatible": sum(1 for r in mat if r["status"] == "incompatible")}
    out["ok"] = not out["errors"]
    return out
