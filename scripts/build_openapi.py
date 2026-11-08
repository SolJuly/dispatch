#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 OpenAPI 3.1 契约（docs/openapi.json）+ 前端 Mock 用的示例数据。

为什么用脚本生成而不是手写 YAML：示例数据要和真实响应同源、可被测试校验
（`tools/p20/tests/test_openapi.py` 会检查 $ref、operationId 唯一、示例齐全，
以及 x-status=implemented 的路径在 fileserver.py 里真的存在）。

用法：
    python3 scripts/build_openapi.py            # 写 docs/openapi.json
    python3 scripts/build_openapi.py --check    # 只校验不写
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "openapi.json"
TOKEN = "23e00ec2"
PUBLIC = f"https://123.207.74.155/files/{TOKEN}"
LOCAL = f"http://127.0.0.1:8099/files/{TOKEN}"

# --------------------------------------------------------------------- 组件
SCHEMAS = {
    "Error": {
        "type": "object", "required": ["error"],
        "properties": {"error": {"type": "string", "description": "可直接展示给用户的错误文案"}},
        "examples": [{"error": "参数/工作流校验失败：params.batch_size 不在工作流 sdxl_inpaint 的参数契约里"}],
    },
    "Artifact": {
        "type": "object", "required": ["name", "bytes", "url"],
        "properties": {
            "name": {"type": "string"},
            "bytes": {"type": "integer", "format": "int64"},
            "url": {"type": "string", "description": "静态路径，进程重启后仍可访问"},
            "published": {"type": "boolean", "description": "是否已复制到 /pub/jobs（false 时可能 404 几秒）"},
            "sha256": {"type": "string", "nullable": True},
            "width": {"type": "integer", "nullable": True},
            "height": {"type": "integer", "nullable": True},
        },
        "examples": [{
            "name": "01-p1-20261103T1219-b2df1a_00001_.png", "bytes": 748650,
            "url": "/pub/jobs/p1-20261103T1219-b2df1a/01-p1-20261103T1219-b2df1a_00001_.png",
            "published": True,
            "sha256": "c65f4c5da2976cdacefbef69fac7318abf97527d9da3e593515f379623f8d857",
            "width": 1024, "height": 1024,
        }],
    },
    "JobSummary": {
        "type": "object",
        "required": ["job_id", "state", "workflow", "created_at"],
        "properties": {
            "job_id": {"type": "string", "pattern": "^p1-[0-9]{8}T[0-9]{4}-[0-9a-f]{6}$"},
            "state": {"type": "string", "description": "CREATED/PROVISIONING/READY/SUBMITTED/RUNNING/FETCHING/VERIFYING/PERSISTED/RELEASING/DONE/FAILED_*/ABORTED"},
            "workflow": {"type": "string"},
            "workflow_version": {"type": "integer"},
            "model": {"type": "string"},
            "created_at": {"type": "string", "format": "date-time"},
            "kind": {"type": "string", "enum": ["image", "video"]},
            "prompt": {"type": "string"},
            "artifacts": {"type": "integer", "description": "产物数量"},
            "first_artifact_url": {"type": "string", "description": "首图静态 URL，空串表示暂无"},
        },
        "examples": [{
            "job_id": "p1-20261103T1219-b2df1a", "state": "DONE",
            "workflow": "sdxl_inpaint", "workflow_version": 1, "model": "sdxl_juggernaut",
            "created_at": "2026-11-03T12:19:47+08:00", "kind": "image",
            "prompt": "a green pear on wooden table, warm sunset light",
            "artifacts": 1,
            "first_artifact_url": "/pub/jobs/p1-20261103T1219-b2df1a/01-p1-20261103T1219-b2df1a_00001_.png",
        }],
    },
    "JobList": {
        "type": "object", "required": ["items", "count"],
        "properties": {"items": {"type": "array", "items": {"$ref": "#/components/schemas/JobSummary"}},
                       "count": {"type": "integer"}},
        "examples": [{
            "count": 2,
            "items": [
                {"job_id": "p1-20261103T1219-b2df1a", "state": "DONE", "workflow": "sdxl_inpaint",
                 "workflow_version": 1, "model": "sdxl_juggernaut",
                 "created_at": "2026-11-03T12:19:47+08:00", "kind": "image",
                 "prompt": "a green pear on wooden table", "artifacts": 1,
                 "first_artifact_url": "/pub/jobs/p1-20261103T1219-b2df1a/01-p1-20261103T1219-b2df1a_00001_.png"},
                {"job_id": "p1-20261103T1245-a47eee", "state": "FAILED_PROVISION",
                 "workflow": "upscale_4x", "workflow_version": 1, "model": "upscale_ultrasharp",
                 "created_at": "2026-11-03T12:45:11+08:00", "kind": "image",
                 "prompt": "", "artifacts": 0, "first_artifact_url": ""},
            ],
        }],
    },
    "JobDetail": {
        "type": "object", "required": ["job", "artifacts"],
        "properties": {
            "job": {"$ref": "#/components/schemas/Job"},
            "artifacts": {"type": "array", "items": {"$ref": "#/components/schemas/Artifact"}},
            "workflow": {"type": "string"},
            "terminal": {"type": "boolean", "description": "是否终态（DONE/FAILED/ABORTED）"},
        },
        "examples": [{
            "job": {
                "job_id": "p1-20261103T1219-b2df1a", "state": "DONE",
                "created_at": "2026-11-03T12:19:47+08:00", "kind": "image",
                "model": {"key": "sdxl_juggernaut", "workflow": "workflows/sdxl_inpaint.api.json",
                          "registry": {"workflow_key": "sdxl_inpaint", "workflow_version": 1,
                                       "model_key": "sdxl_juggernaut", "model_version": 1}},
                "params": {"image": "in-a6891d7b.png", "mask": "in-5935a044.png",
                           "prompt": "a green pear on wooden table", "denoise": 0.85,
                           "seed": 11, "steps": 25, "cfg": 6.0},
                "params_hash": "sha256:49895ef17c1f...", "expect": {"ext": "png", "min_bytes": 20000},
                "steps": {"provision": {"done": True, "at": "2026-11-03T12:20:07+08:00"},
                          "submit": {"done": True, "at": "2026-11-03T12:21:05+08:00"},
                          "wait": {"done": True, "at": "2026-11-03T12:21:30+08:00"}},
                "state_seq": 11,
            },
            "artifacts": [{
                "name": "01-p1-20261103T1219-b2df1a_00001_.png", "bytes": 748650,
                "url": "/pub/jobs/p1-20261103T1219-b2df1a/01-p1-20261103T1219-b2df1a_00001_.png",
                "published": True, "sha256": "c65f4c5d...", "width": 1024, "height": 1024,
            }],
            "workflow": "sdxl_inpaint", "terminal": True,
        }],
    },
    "Job": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"}, "state": {"type": "string"},
            "created_at": {"type": "string", "format": "date-time"},
            "kind": {"type": "string"},
            "model": {"type": "object"},
            "params": {"type": "object", "additionalProperties": True},
            "params_hash": {"type": "string"},
            "expect": {"type": "object", "additionalProperties": True},
            "steps": {"type": "object", "additionalProperties": True},
            "state_seq": {"type": "integer"},
        },
    },
    "CreateJobRequest": {
        "type": "object", "required": ["workflow", "params"],
        "properties": {
            "workflow": {"type": "string", "description": "registry 里的工作流 key"},
            "params": {"type": "object", "additionalProperties": True,
                       "description": "按工作流参数契约传；服务端补默认值并校验，多传键会被拒"},
            "run": {"type": "boolean", "default": False,
                    "description": "true 时后台立即执行（占 GPU、产生费用）"},
            "note": {"type": "string", "description": "备注，写入 task_ref"},
        },
        "examples": [{
            "workflow": "sdxl_t2i", "run": False,
            "params": {"prompt": "a red apple on a wooden table", "seed": 42,
                       "width": 1024, "height": 1024, "steps": 25, "cfg": 6.0,
                       "sampler": "dpmpp_2m", "scheduler": "karras", "batch_size": 2},
        }],
    },
    "CreateJobResponse": {
        "type": "object", "required": ["job_id", "state"],
        "properties": {
            "job_id": {"type": "string"}, "state": {"type": "string"},
            "run": {"type": "boolean"},
            "workflow": {"type": "string"}, "workflow_version": {"type": "integer"},
            "model": {"type": "string"}, "params_hash": {"type": "string"},
            "params": {"type": "object", "additionalProperties": True,
                       "description": "补全默认值后的最终参数"},
            "expect": {"type": "object", "additionalProperties": True},
        },
        "examples": [{
            "job_id": "p1-20261103T130000-a1b2c3", "state": "CREATED", "run": False,
            "workflow": "sdxl_t2i", "workflow_version": 1, "model": "sdxl_juggernaut",
            "params_hash": "sha256:2de9ed823a22...",
            "params": {"prompt": "a red apple on a wooden table", "seed": 42, "width": 1024,
                       "height": 1024, "steps": 25, "cfg": 6.0, "sampler": "dpmpp_2m",
                       "scheduler": "karras", "batch_size": 2, "neg_prompt": ""},
            "expect": {"ext": "png", "min_bytes": 20000, "images": 2,
                       "width": [1024], "height": [1024], "black_ratio_max": 0.5},
        }],
    },
    "UploadResponse": {
        "type": "object", "required": ["name", "bytes", "url"],
        "properties": {
            "name": {"type": "string", "description": "内容哈希文件名，提交时作为 params.image / params.mask"},
            "bytes": {"type": "integer"},
            "url": {"type": "string", "description": "原图预览地址（/f/inputs/...）"},
        },
        "examples": [{"name": "in-a6891d7b.png", "bytes": 810226, "url": "/f/inputs/in-a6891d7b.png"}],
    },
    "QueueStats": {
        "type": "object",
        "properties": {"total": {"type": "integer"}, "running": {"type": "integer"},
                       "queued": {"type": "integer"}, "done": {"type": "integer"},
                       "failed": {"type": "integer"}},
        "examples": [{"total": 19, "running": 0, "queued": 0, "done": 15, "failed": 4}],
    },
    "QueueJob": {
        "type": "object",
        "properties": {"job_id": {"type": "string"}, "state": {"type": "string"},
                       "model": {"type": "string"}, "kind": {"type": "string"},
                       "attempts": {"type": "integer"}, "defers": {"type": "integer"},
                       "cost": {"type": "number"}, "gpu_minutes": {"type": "number"},
                       "verdict": {"type": "string", "nullable": True},
                       "artifacts": {"type": "array", "items": {"type": "string"}},
                       "enqueued_at": {"type": "string"}, "ended_at": {"type": "string"}},
        "examples": [{
            "job_id": "p1-20261103T1219-b2df1a", "queue_state": "DONE", "job_state": "DONE",
            "model": "sdxl_juggernaut", "kind": "image", "attempts": 1, "defers": 0,
            "cost": 0.08, "gpu_minutes": 2.2, "verdict": "PASS",
            "artifacts": ["reports/p1/jobs/p1-20261103T1219-b2df1a/artifacts/01-....png"],
            "enqueued_at": "2026-11-03T12:19:47+08:00", "ended_at": "2026-11-03T12:22:10+08:00",
        }],
    },
    "RegistryWorkflow": {
        "type": "object",
        "properties": {
            "key": {"type": "string"}, "name": {"type": "string"},
            "version": {"type": "integer"}, "kind": {"type": "string"},
            "params": {"type": "object", "description": "参数契约（type/default/min/max/values/bindings）",
                       "additionalProperties": True},
            "compat": {"$ref": "#/components/schemas/Compat"},
        },
        "examples": [{
            "key": "sdxl_t2i", "name": "SDXL 文生图 1024×1024（快速基线）", "version": 1,
            "kind": "image",
            "params": {"prompt": {"type": "string", "required": True},
                       "batch_size": {"type": "int", "default": 1, "min": 1, "max": 4}},
            "compat": {"status": "verified", "basis": "real-run", "evidence": ["p1-20261103T0059-998e34"]},
        }],
    },
    "RegistryModel": {
        "type": "object",
        "properties": {"key": {"type": "string"}, "name": {"type": "string"},
                       "version": {"type": "integer"}, "kind": {"type": "string"},
                       "family": {"type": "string"},
                       "files": {"type": "object", "additionalProperties": True},
                       "constraints": {"type": "object", "additionalProperties": True}},
        "examples": [{
            "key": "sdxl_juggernaut", "name": "SDXL juggernautXL v8", "version": 1,
            "kind": "image", "family": "sdxl",
            "files": {"ckpt": {"name": "juggernautXL_v8Rundiffusion_V8.safetensors",
                               "loader": "CheckpointLoaderSimple", "input": "ckpt_name"}},
            "constraints": {"width": {"min": 512, "max": 1536, "multiple": 8}},
        }],
    },
    "Compat": {
        "type": "object",
        "properties": {"status": {"type": "string", "enum": ["verified", "equivalent", "experimental", "incompatible"]},
                       "basis": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}},
                       "notes": {"type": "string"}},
        "examples": [{"status": "verified", "basis": "real-run",
                      "evidence": ["p1-20261103T1219-b2df1a"],
                      "notes": "M3 首次真机：蒙版外平均差 1.17/255"}],
    },
    "VersionInfo": {
        "type": "object", "required": ["service", "api", "contract", "features"],
        "properties": {
            "service": {"type": "string"},
            "ok": {"type": "boolean"},
            "api": {"type": "string", "description": "后端 API 版本；P2.1 = \"p2.1\""},
            "contract": {"type": "string", "description": "配套契约版本（与本文档同源）"},
            "build": {"type": "object", "additionalProperties": True,
                      "description": "commit / branch / dirty / dirty_files"},
            "started_at": {"type": "string", "format": "date-time"},
            "uptime_s": {"type": "integer"},
            "pid": {"type": "integer"},
            "bind": {"type": "string", "description": "监听地址，便于确认打的是哪个进程"},
            "base": {"type": "string"},
            "root": {"type": "string", "description": "分享根目录"},
            "capabilities": {"type": "array", "items": {"type": "string"}},
            "features": {"type": "object", "additionalProperties": {"type": "boolean"},
                         "description": "能力表；前端按它判断能不能用某一组路由"},
            "registry": {"type": "object", "additionalProperties": True},
            "docs": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        "examples": [{
            "service": "dsh-share", "ok": True, "api": "p2.1", "contract": "openapi-3.1",
            "build": {"commit": "3180f15", "branch": "feature-/creative-engine",
                      "dirty": True, "dirty_files": 4},
            "started_at": "2026-11-03T13:34:34+08:00", "uptime_s": 42, "pid": 8123,
            "bind": "127.0.0.1:8081", "base": "/files",
            "root": "/root/智云视频工作流/share",
            "capabilities": ["console", "registry", "jobs", "uploads", "zip", "version"],
            "features": {"console": True, "registry": True, "jobs": True,
                         "uploads": True, "zip": True, "version": True},
            "registry": {"workflows": 13, "models": 6},
            "docs": {"openapi": "/pub/api/openapi.json",
                     "handoff": "/pub/p21/07-API契约与Mock.html"},
        }],
    },
    "RegistrySummary": {
        "type": "object",
        "properties": {"workflows": {"type": "integer"}, "models": {"type": "integer"},
                       "compat": {"type": "object", "additionalProperties": {"type": "integer"}}},
        "examples": [{"workflows": 13, "models": 6,
                      "compat": {"verified": 10, "equivalent": 2, "experimental": 1}}],
    },
    "ResolveResponse": {
        "type": "object",
        "properties": {"workflow": {"type": "string"}, "workflow_version": {"type": "integer"},
                       "model": {"type": "string"}, "compat": {"$ref": "#/components/schemas/Compat"},
                       "params": {"type": "object", "additionalProperties": True},
                       "defaults_applied": {"type": "array", "items": {"type": "string"}},
                       "expect": {"type": "object", "additionalProperties": True},
                       "render": {"type": "object", "additionalProperties": True}},
        "examples": [{
            "workflow": "sdxl_t2i", "workflow_version": 1, "model": "sdxl_juggernaut",
            "compat": {"status": "verified", "basis": "real-run", "evidence": []},
            "params": {"prompt": "a red apple", "seed": 42, "batch_size": 2, "steps": 25},
            "defaults_applied": ["cfg", "height", "width", "sampler", "scheduler"],
            "expect": {"ext": "png", "images": 2, "width": [1024], "height": [1024]},
            "render": {"nodes": 7, "graph_sha256": "e7174e489b254c72..."},
        }],
    },
    "Preset": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "name": {"type": "string"},
                       "workflow": {"type": "string"},
                       "params": {"type": "object", "additionalProperties": True},
                       "created_at": {"type": "string", "format": "date-time"}},
        "examples": [{"id": "pre_01", "name": "厚涂人像", "workflow": "sdxl_t2i_lora",
                      "params": {"lora": "Sdxl/绪儿_厚涂构图光影质感增强V3.safetensors",
                                 "lora_strength": 1.0},
                      "created_at": "2026-11-03T13:00:00+08:00"}],
    },
}

PARAMS = {
    "token": {"name": "token", "in": "path", "required": True,
              "schema": {"type": "string"}, "description": "访问 token（路径鉴权，不是 Cookie）"},
    "job_id": {"name": "job_id", "in": "path", "required": True,
               "schema": {"type": "string", "pattern": "^p1-[0-9]{8}T[0-9]{4}-[0-9a-f]{6}$"}},
}


def op(*, oid, tag, summary, status="implemented", desc="", params=(), body=None,
       resp=None, resp_code="200", resp_desc="成功"):
    if resp:
        success = {"description": resp_desc,
                   "content": {"application/json": {
                       "schema": {"$ref": f"#/components/schemas/{resp}"},
                       "examples": {"default": {"value": SCHEMAS[resp]["examples"][0]}}}}}
    else:
        success = {"description": resp_desc,
                   "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}
    o = {
        "tags": [tag], "summary": summary, "operationId": oid, "x-status": status,
        "responses": {
            resp_code: success,
            "400": {"$ref": "#/components/responses/BadRequest"},
            "404": {"$ref": "#/components/responses/NotFound"},
            "500": {"$ref": "#/components/responses/ServerError"},
        },
    }
    if desc:
        o["description"] = desc
    if params:
        o["parameters"] = [{"$ref": f"#/components/parameters/{p}"} for p in params]
    if body:
        o["requestBody"] = {"required": True, "content": {"application/json": {
            "schema": {"$ref": f"#/components/schemas/{body}"},
            "examples": {"default": {"value": SCHEMAS[body]["examples"][0]}}}}}
    return o


PATHS = {
    "/api/p2/jobs": {
        "get": op(oid="listJobs", tag="jobs", summary="作业列表（文件视角）",
                  desc="按创建时间倒序。`state=FAILED` 会前缀匹配所有 FAILED_*。",
                  params=(), resp="JobList"),
        "post": op(oid="createJob", tag="jobs", summary="创建作业（唯一写入口）",
                   desc="服务端用 registry 补默认值并校验；experimental 组合返回 409。"
                        "`run=true` 时后台起 run.py，日志写 job 目录 run.log。",
                   body="CreateJobRequest", resp="CreateJobResponse", resp_code="201",
                   resp_desc="已创建"),
    },
    "/api/p2/jobs/{job_id}": {
        "get": op(oid="getJob", tag="jobs", summary="作业详情（含产物与时间线）",
                  params=("token", "job_id"), resp="JobDetail"),
    },
    "/api/p2/jobs/{job_id}/cancel": {
        "post": op(oid="cancelJob", tag="jobs", summary="取消尚未开始的作业",
                   desc="仅 CREATED 可取消（置 ABORTED，state_seq+1）；已执行返回 409。",
                   params=("token", "job_id"), resp="CreateJobResponse"),
    },
    "/api/p2/jobs/{job_id}/rerun": {
        "post": op(oid="rerunJob", tag="jobs", summary="同参重跑（P2.2 计划）",
                   status="planned",
                   desc="复制原作业参数生成新作业；`overrides` 可覆盖部分参数。",
                   params=("token", "job_id"), resp="CreateJobResponse", resp_code="201"),
    },
    "/api/p2/jobs/{job_id}/artifacts/{name}": {
        "get": op(oid="getArtifact", tag="files", summary="下载单个产物（P2.2 计划）",
                  status="planned",
                  desc="当前前端直接用 artifact.url（/pub/jobs/...）静态地址；此接口用于鉴权下载。",
                  params=("token", "job_id"), resp="Artifact"),
    },
    "/api/p2/jobs/zip": {
        "get": op(oid="downloadJobsZip", tag="files", summary="批量下载选中作业的产物",
                  desc="`ids` 逗号分隔；内存打包，优先取已发布静态副本；一个都没有时 404。",
                  resp=None),
    },
    "/api/p2/uploads": {
        "post": op(oid="uploadImage", tag="uploads", summary="上传输入图/蒙版（multipart）",
                   desc="字段名 `file`；PNG/JPEG/WebP，≤20MB，按内容哈希命名 `in-<sha8>.<ext>`；"
                        "同时写作业输入目录与 /f/inputs/。非图片 415，超限 413。",
                   resp="UploadResponse", resp_code="201"),
    },
    "/api/p2/queue": {
        "get": op(oid="getQueue", tag="queue", summary="队列统计（旧契约）", resp="QueueStats"),
    },
    "/api/p2/queue/jobs": {
        "get": op(oid="listQueueJobs", tag="queue", summary="队列视角的作业列表（旧契约）",
                  desc="P2.1 之前 `/api/p2/jobs` 的契约，现在挪到这里；新前端请用 `/api/p2/jobs`。",
                  resp="JobList"),
    },
    "/api/p2/queue/jobs/{job_id}": {
        "get": op(oid="getQueueJob", tag="queue", summary="队列视角的作业详情（旧契约）",
                  params=("token", "job_id"), resp="QueueJob"),
    },
    "/api/p2/audit": {
        "get": op(oid="listAudit", tag="queue", summary="审计事件（增量拉取）",
                  desc="轮询用 `since` 游标（audit id）。", resp="JobList"),
    },
    "/api/p2/events": {
        "get": op(oid="listEvents", tag="queue", summary="事件流（增量拉取）",
                  desc="轮询用 `since` 游标（events seq）。", resp="JobList"),
    },
    "/api/p2/version": {
        "get": op(oid="getVersion", tag="status", summary="后端版本与能力探测（前端启动先打）",
                  desc="返回构建 commit、API/契约版本、features 能力表。旧后端（P2.1 之前）"
                       "没有这条路由 → 404，前端应提示改用 Mock 或等待重新部署。",
                  resp="VersionInfo"),
    },
    "/api/p2/status": {
        "get": op(oid="getStatus", tag="status", summary="系统状态（队列/成本/实例数）", resp="RegistrySummary"),
    },
    "/api/p2/registry": {
        "get": op(oid="getRegistrySummary", tag="registry", summary="Registry 统计摘要", resp="RegistrySummary"),
    },
    "/api/p2/registry/workflows": {
        "get": op(oid="listWorkflows", tag="registry", summary="工作流列表（含参数契约与兼容性）",
                  desc="控制台的模型下拉数据源；`kind=image` 可过滤。",
                  resp="RegistryWorkflow"),
    },
    "/api/p2/registry/workflows/{key}": {
        "get": op(oid="getWorkflow", tag="registry", summary="工作流详情",
                  params=("token",), resp="RegistryWorkflow"),
    },
    "/api/p2/registry/models": {
        "get": op(oid="listModels", tag="registry", summary="模型列表", resp="RegistryModel"),
    },
    "/api/p2/registry/models/{key}": {
        "get": op(oid="getModel", tag="registry", summary="模型详情", params=("token",), resp="RegistryModel"),
    },
    "/api/p2/registry/compat": {
        "get": op(oid="getCompatMatrix", tag="registry", summary="兼容性矩阵", resp="RegistrySummary"),
    },
    "/api/p2/registry/stats": {
        "get": op(oid="getRegistryStats", tag="registry", summary="Registry 统计", resp="RegistrySummary"),
    },
    "/api/p2/registry/resolve": {
        "get": op(oid="resolveWorkflow", tag="registry", summary="预演：补默认值 + 渲染 + 期望值",
                  desc="不落盘、不建作业；`params` 为 URL-encoded JSON，`graph=1` 返回完整图。",
                  resp="ResolveResponse"),
    },
    "/api/p2/presets": {
        "get": op(oid="listPresets", tag="presets", summary="参数预设列表（P2.2 计划）",
                  status="planned", resp="Preset"),
        "post": op(oid="createPreset", tag="presets", summary="保存参数预设（P2.2 计划）",
                   status="planned", body="Preset", resp="Preset", resp_code="201"),
    },
    "/api/p2/share/{job_id}": {
        "get": op(oid="getSharedJob", tag="share", summary="公开分享页数据（P2.2 计划）",
                  status="planned",
                  desc="签名访问（`sig`），不暴露主 token；只返回该作业的产物与参数摘要。",
                  params=("token", "job_id"), resp="JobDetail"),
    },
}


def build() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "AI 图片/视频生产平台 · 后端接口",
            "version": "1.0.0",
            "description": (
                "覆盖 P2 只读接口、P2.0 Registry、P2.1 图片控制台（作业/上传/下载）。\n\n"
                "**鉴权**：token 在路径里（`/api/p2/...` 前一段），不是 Cookie/Header。\n\n"
                "**套接字**：见 `servers`。`/files/<TOKEN>` 由 cs-files 进程提供。\n\n"
                "**版本探测**：前端启动先打 `GET /api/p2/version`；404 就说明线上是旧后端，别继续调。\n\n"
                "**x-status**：`implemented` = 已上线可直接联调；`planned` = 契约先定，前端先用 Mock。\n\n"
                "**Mock**：`python3 scripts/mock_api.py` 会按本文件的 examples 起一个可写假的本地服务。\n\n"
                "**产物 URL**：`artifact.url` 指向 `/pub/jobs/...` 静态路径，服务重启后仍可访问。"
            ),
        },
        "servers": [
            {"url": PUBLIC, "description": "线上（临时进程，可能重启）"},
            {"url": LOCAL, "description": "本地 Mock（scripts/mock_api.py）"},
        ],
        "tags": [
            {"name": "jobs", "description": "作业创建/查询/取消"},
            {"name": "uploads", "description": "输入图与蒙版"},
            {"name": "files", "description": "产物下载"},
            {"name": "queue", "description": "队列与审计（旧契约）"},
            {"name": "status", "description": "系统状态"},
            {"name": "registry", "description": "工作流/模型注册表"},
            {"name": "presets", "description": "参数预设（规划中）"},
            {"name": "share", "description": "公开分享（规划中）"},
        ],
        "components": {
            "schemas": SCHEMAS,
            "parameters": PARAMS,
            "responses": {
                "BadRequest": {"description": "参数/契约错误",
                               "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"},
                                                                "examples": {"default": {"value": {"error": "参数校验失败：params.batch_size 不在工作流契约里"}}}}}},
                "NotFound": {"description": "不存在",
                             "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"},
                                                              "examples": {"default": {"value": {"error": "作业不存在：p1-20260101T0000-ffffff"}}}}}},
                "ServerError": {"description": "服务端错误",
                                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"},
                                                                 "examples": {"default": {"value": {"error": "RuntimeError: ..."}}}}}},
            },
        },
        "paths": PATHS,
        "x-mock": {"token": TOKEN, "note": "scripts/mock_api.py 读取本字段决定 token 段"},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    spec = build()
    if not a.check:
        OUT.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[ok] {OUT.relative_to(ROOT)} · {len(spec['paths'])} paths · "
              f"{len(spec['components']['schemas'])} schemas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
