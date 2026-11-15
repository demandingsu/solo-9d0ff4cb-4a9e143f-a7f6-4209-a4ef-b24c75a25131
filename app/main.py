"""FastAPI 入口：字段变更核查 + 观测契约校验，静态页面与查询接口。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .contract import (ContractError, check_compatible, contract_dict,
                       parse_contract, quarantine_dicts, validate_csv)
from .evaluator import FormatError, build_report, parse_dependencies, parse_sources
from .store import ContractStore, SnapshotStore

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
EXAMPLES_DIR = Path(os.environ.get("EXAMPLES_DIR", BASE_DIR.parent / "examples"))
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR.parent / "data" / "dwcheck.db"))

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 单文件 20MB 上限

store = SnapshotStore(DB_PATH)
contract_store = ContractStore(DB_PATH)
app = FastAPI(title="环境监测数仓字段变更核查 + 观测契约校验", version="1.1.0")


async def _read_json(upload: UploadFile, label: str) -> Any:
    raw = await upload.read()
    if not raw:
        raise FormatError(f"{label}：文件为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise FormatError(f"{label}：文件超过 20MB 上限")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise FormatError(f"{label}：不是合法的 JSON 文件（{e}）") from e


def build_payload(name: str, sources_before_raw: Any, sources_after_raw: Any,
                  dependencies_raw: Any) -> dict:
    """解析 -> 评估 -> 组装自包含快照内容。任何 FormatError 都在此抛出。"""
    src_before = parse_sources(sources_before_raw)
    src_after = parse_sources(sources_after_raw)
    deps = parse_dependencies(dependencies_raw)
    report = build_report(deps, src_before, src_after)
    return {
        "name": name,
        "sources_before": sources_before_raw,
        "sources_after": sources_after_raw,
        "dependencies": dependencies_raw,
        "report": report,
    }


@app.post("/api/snapshots")
async def create_snapshot(
    sources_before: UploadFile = File(..., description="变更前源字段清单 JSON"),
    sources_after: UploadFile = File(..., description="变更后源字段清单 JSON"),
    dependencies: UploadFile = File(..., description="跨作业字段依赖 JSON"),
    name: str = Form(""),
):
    """上传三份清单并核查。成功则整版替换现役版本；失败保留旧版并留痕。"""
    snap_name = name.strip() or f"核查-{dependencies.filename or '未命名'}"
    try:
        payload = build_payload(
            snap_name,
            await _read_json(sources_before, "变更前源字段清单"),
            await _read_json(sources_after, "变更后源字段清单"),
            await _read_json(dependencies, "跨作业字段依赖"),
        )
    except FormatError as e:
        store.record_failed(snap_name, str(e))
        raise HTTPException(status_code=400, detail=str(e)) from e
    snap_id = store.publish(snap_name, payload)
    return {"id": snap_id, "name": snap_name, "summary": payload["report"]["summary"]}


@app.get("/api/snapshots/current")
async def get_current():
    """现役版本全量内容（查询只命中单一版本，绝不混版）。"""
    cur = store.current()
    if not cur:
        raise HTTPException(status_code=404, detail="暂无现役版本，请先上传核查")
    return cur


@app.get("/api/snapshots")
async def list_snapshots():
    return {"items": store.history()}


@app.get("/api/health")
async def health():
    return {"status": "up"}


# ---------------------------------------------------------------------------
# 观测数据契约：发布（仅兼容演进）、查询
# ---------------------------------------------------------------------------

async def _read_csv(upload: UploadFile, label: str) -> str:
    raw = await upload.read()
    if not raw:
        raise HTTPException(status_code=400, detail=f"{label}：文件为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"{label}：文件超过 20MB 上限")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400,
                            detail=f"{label}：不是合法的 UTF-8 文本（{e}）") from e


@app.post("/api/contracts")
async def upload_contract(contract: UploadFile = File(..., description="带版本号的契约 JSON")):
    """上传新契约。仅当相对现役契约是兼容演进（新增可选列/放宽约束）时才替换；
    删列、改类型、收紧约束一律拒绝并保留旧版，拒绝记录留痕。"""
    try:
        data = await _read_json(contract, "契约文件")
    except FormatError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        new_contract = parse_contract(data)
    except ContractError as e:
        version = data.get("version") if isinstance(data, dict) else None
        contract_store.reject_contract(
            version.strip() if isinstance(version, str) and version.strip() else "(未解析)",
            f"契约格式错误：{e}")
        raise HTTPException(status_code=400, detail=f"契约格式错误：{e}") from e
    active = contract_store.active_contract()
    if active:
        old_contract = parse_contract(active["payload"])
        problems = check_compatible(old_contract, new_contract)
        if problems:
            msg = "；".join(problems)
            contract_store.reject_contract(new_contract.version, msg)
            raise HTTPException(
                status_code=400,
                detail=f"新契约不兼容，已拒绝并保留现役契约 v{active['version']}：{msg}")
    cid = contract_store.publish_contract(new_contract.version, new_contract.raw)
    return {"id": cid, "version": new_contract.version,
            "columns": len(new_contract.columns),
            "replaced": active["version"] if active else None}


@app.get("/api/contracts/current")
async def get_current_contract():
    """现役契约（查询只命中 active 的唯一版本）。"""
    cur = contract_store.active_contract()
    if not cur:
        raise HTTPException(status_code=404, detail="暂无现役契约，请先上传发布")
    parsed = parse_contract(cur["payload"])
    return {"id": cur["id"], "version": cur["version"],
            "created_at": cur["created_at"], **contract_dict(parsed)}


@app.get("/api/contracts")
async def list_contracts():
    return {"items": contract_store.contract_history()}


# ---------------------------------------------------------------------------
# 观测批次：按现役契约校验，全量通过才发布，失败隔离且不影响上次可用批次
# ---------------------------------------------------------------------------

@app.post("/api/batches")
async def upload_batch(
    observations: UploadFile = File(..., description="观测记录 CSV"),
    name: str = Form(""),
):
    """上传观测 CSV，按上传时的现役契约快照逐行校验。

    全量通过 => 发布（成为新的上次可用批次）；任何错误 => 整批隔离留痕，
    上次可用批次不变。两种结果都绑定校验时的契约快照，响应 200 并以
    state 字段区分 published / quarantined。"""
    active = contract_store.active_contract()
    if not active:
        raise HTTPException(status_code=409, detail="暂无现役契约，请先发布契约再上传批次")
    contract = parse_contract(active["payload"])
    text = await _read_csv(observations, "观测 CSV")
    result = validate_csv(contract, text)
    batch_name = name.strip() or (observations.filename or "未命名批次")
    base = {"name": batch_name, "contract_version": active["version"],
            "summary": result.summary()}
    if result.ok:
        bid = contract_store.publish_batch(batch_name, active,
                                           result.total_rows, result.rows)
        return {"id": bid, "state": "published", **base}
    bad = quarantine_dicts(result)
    bid = contract_store.quarantine_batch(batch_name, active, result.total_rows,
                                          result.error_rows, result.header_errors, bad)
    return {"id": bid, "state": "quarantined", **base,
            "header_errors": result.header_errors, "quarantine": bad}


@app.get("/api/batches/current")
async def get_current_batch():
    """上次可用批次（最新发布成功的批次）及其绑定的契约快照。"""
    cur = contract_store.current_batch()
    if not cur:
        raise HTTPException(status_code=404, detail="暂无已发布批次")
    return cur


@app.get("/api/batches")
async def list_batches():
    return {"items": contract_store.batch_history()}


@app.get("/api/batches/{batch_id}")
async def get_batch(batch_id: int):
    """批次详情（含隔离清单/类型化记录与其绑定的契约快照，绝不混用现役契约）。"""
    batch = contract_store.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch


@app.get("/contract")
async def contract_page():
    return FileResponse(STATIC_DIR / "contract.html")


@app.get("/examples/{filename}")
async def get_example(filename: str):
    if Path(filename).name != filename or Path(filename).suffix not in {".json", ".csv"}:
        raise HTTPException(status_code=404, detail="未知示例文件")
    path = EXAMPLES_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="示例文件未部署")
    media = "text/csv" if path.suffix == ".csv" else "application/json"
    return FileResponse(path, media_type=media, filename=filename)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
