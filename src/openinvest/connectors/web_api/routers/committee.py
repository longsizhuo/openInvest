"""committee 路由 — 从 web_api.py 按 tag 拆分（行为不变）。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse

from openinvest.connectors.web_api.models import (
    CommitteePrepareRequest,
    CommitteeRunRequest,
    CommitteeRunResponse,
    CommitteeSaveRequest,
    CommitteeStatusResponse,
)

log = logging.getLogger("web_api")
from openinvest.connectors.web_api.routers.write import _now_iso

router = APIRouter()


# ============ 委员会异步任务 ============

# task store 落盘根目录（fcntl 锁由 MemoryStore 复用）
from openinvest.paths import INVEST_ROOT
COMMITTEE_DIR = INVEST_ROOT / "memory" / ".committee"


def _committee_status_path(task_id: str) -> Path:
    return COMMITTEE_DIR / task_id / "status.json"


def _committee_meta_path(task_id: str) -> Path:
    """审计 trail：runtime metadata（commit hash / model / temperature / 等）"""
    return COMMITTEE_DIR / task_id / "meta.json"


# 模块级 cache：commit hash 跑一次 git 就够（lru_cache 在 multiprocessing 跨进程
# 不共享，但每个进程跑一次也只是 ~10ms 的 subprocess）
import functools as _functools
import subprocess as _subprocess
import sys as _sys


@_functools.lru_cache(maxsize=1)
def _audit_commit_hash() -> str:
    """git rev-parse --short HEAD —— 失败返回 'unknown' 而不是抛异常"""
    try:
        result = _subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True,
            cwd=str(INVEST_ROOT),
            timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:  # noqa: BLE001 git 不存在/不在仓库等都视为 unknown
        pass
    return "unknown"


def _build_audit_meta(
    task_id: str,
    symbols: Optional[List[str]],
    max_rounds: int,
) -> Dict[str, Any]:
    """采集本次 committee 跑的运行环境快照，落审计 trail

    关键字段（合规视角）：
    - commit_hash: 跑这次的代码版本
    - model + temperature: LLM 运行参数（决定 verdict 的可复现性）
    - max_debate_rounds: 辩论轮数上限
    - python_version: 运行时
    - symbols: 这次跑了哪些资产
    """
    # 走 utils.llm 拿当前 model（向后兼容 DEEPSEEK_MODEL；支持 LLM_MODEL 切千问/智谱）
    from openinvest.utils.llm import get_llm_config_safe
    _ak, _bu, _model, _provider = get_llm_config_safe()
    return {
        "task_id": task_id,
        "started_at": _now_iso(),
        "commit_hash": _audit_commit_hash(),
        "python_version": _sys.version.split()[0],
        "model": _model,
        "model_temperature": float(os.getenv("INVEST_LLM_TEMPERATURE", "0.2")),
        "max_debate_rounds": max_rounds,
        "symbols": symbols if symbols else "(strategy.target_assets all)",
        "executor": "openinvest.core.committee_runner.run_committee_for_symbol",
        "provider": "deepseek (Web/Cron path)",  # 物理通道：OpenAI 兼容协议；具体模型见 model 字段
    }


# v3 真并行后多线程并发写 status.json
# _status_locks / write / read 已抽到 connectors/state_bus.py 单例模块，
# 这里只做 import alias，让后续 web_api 内部代码不需要改变调用名。
from openinvest.connectors.state_bus import (
    write_committee_status as _write_committee_status,
    read_committee_status as _read_committee_status,
)


def _run_committee_task(
    task_id: str,
    symbols: Optional[List[str]],
    max_rounds: int,
    event_ids: Optional[List[str]] = None,
) -> None:
    """v3 真并行：多资产同时跑，共享 macro_view，progress 实时推 status.json

    流程：
      1. macro_view 跑 1 次（跨资产共享）
      2. ThreadPoolExecutor 并行跑每个 symbol 的 run_committee_for_symbol
         （max_workers 限 4 防 LLM API 限流）
      3. 每个资产内部 Round 1/2 内 Quant + Risk 已并行（committee.py 实现）
    """
    cur = _read_committee_status(task_id) or {}
    cur.update({"status": "running", "running_at": _now_iso(), "events": []})
    _write_committee_status(task_id, cur)

    # 审计 trail：runtime metadata 落 meta.json，永不被 progress 写覆盖
    audit_meta = _build_audit_meta(task_id, symbols, max_rounds)
    if event_ids:
        audit_meta["triggered_by_event_ids"] = event_ids
    try:
        meta_path = _committee_meta_path(task_id)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps(audit_meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001 审计落盘失败不能阻断业务
        log.warning(f"audit meta.json 落盘失败 task_id={task_id}: {e}")

    def on_progress(event: Dict[str, Any]) -> None:
        s = _read_committee_status(task_id) or {}
        events = s.get("events") or []
        events.append({"ts": _now_iso(), **event})
        s["events"] = events[-200:]    # 多资产时 events 量大，限 200
        s["phase"] = event.get("phase")
        s["last_event"] = event
        s["status"] = "running"
        _write_committee_status(task_id, s)

    try:
        from openinvest.core.committee_runner import run_committee_session

        # 三路径统一架构：所有跨资产 macro 共享 + event_brief 召回/翻译 + 并行
        # dispatch 都在 run_committee_session 内部一处实现，避免跟 Cron/Skill 漂移。
        # 历史：之前这里 90 行手搓的 orchestrator 跟 daily_report 复制了一份，
        # 加新参数总漏一处。修复 2026-05-16: 整段迁移到 service 层。
        # 本函数是纯同步体（无 await），由 committee_run 用 daemon 线程拉起
        # （见调用处），故这里直接同步调 session 不会阻塞 uvicorn 事件循环。
        # 历史（#105）：曾是 async def + asyncio.create_task，同步 session 在事件
        # 循环线程上跑 → 整服务假死 5-10 分钟。之前试过 run_in_executor/to_thread/
        # Event/Queue 都在 anyio TestClient 下失败，根因是它们都想把结果桥回事件
        # 循环；本任务只通过 status.json 文件通信、不回桥任何 asyncio 原语，所以
        # 改成"独立 OS 线程跑纯同步体"彻底绕开该不兼容，测试与生产行为一致。
        session = run_committee_session(
            symbols=symbols,
            max_debate_rounds=max_rounds,
            progress_callback=on_progress,
            event_ids=event_ids,
        )
        symbols = session["symbols"]
        results = session["asset_committees"]

        # 提取 verdict 摘要（避免序列化整个 CommitteeReport 对象）
        summary = {}
        for sym, res in results.items():
            if isinstance(res, dict):
                summary[sym] = {
                    "verdict": res.get("verdict"),
                    # CLI run_committee 输出含 cio_memo（Markdown，agent 渲染给用户）；
                    # web 路径补齐，远端模式下客户端轮询 done 后直接拿到同款字段
                    "cio_memo": (
                        res["report"].cio_memo
                        if res.get("report") is not None else None
                    ),
                    "debate_meta": (
                        {k: v for k, v in (res.get("debate") or {}).items()
                         if k not in {"quant_history", "risk_history"}}
                        if res.get("debate") else None
                    ),
                    "error": res.get("error"),
                }

        s = _read_committee_status(task_id) or {}
        s.update({
            "status": "done",
            "ended_at": _now_iso(),
            "phase": "done",
            "result": {
                "symbols": symbols,
                "max_debate_rounds": max_rounds,
                "by_asset": summary,
            },
        })
        _write_committee_status(task_id, s)

        # 事件触发的委员会跑完 → 补发 verdict 邮件（修复 event_watch → 委员会 →
        # verdict 邮件断链：web 路径本身不发邮件，event 预警里"verdict 邮件随后送达"
        # 之前是空头支票。GUI 手动触发 event_ids 为空 → 不发，不打扰）。
        if event_ids:
            try:
                from openinvest.services.event_notifier import send_committee_verdict_email
                send_committee_verdict_email(
                    task_id=task_id,
                    symbols=symbols or [],
                    by_asset=summary,
                    event_ids=event_ids,
                )
                log.info(f"committee {task_id} 事件触发 → verdict 邮件已发")
            except Exception as e:  # noqa: BLE001  邮件失败不能阻断任务/状态
                log.warning(
                    f"committee {task_id} verdict 邮件发送失败: {type(e).__name__}: {e}"
                )
    except Exception as e:  # noqa: BLE001
        log.exception(f"committee task {task_id} 失败")
        err = _read_committee_status(task_id) or {}
        err.update({
            "status": "error",
            "ended_at": _now_iso(),
            "phase": "error",
            "error": f"{type(e).__name__}: {e}",
        })
        _write_committee_status(task_id, err)


@router.post("/api/committee/run", response_model=CommitteeRunResponse, tags=["committee"])
async def committee_run(body: CommitteeRunRequest = Body(default=CommitteeRunRequest())) -> CommitteeRunResponse:
    """v3 真并行委员会触发（统一端点）

    - 不传 symbols → 跑 strategy.target_assets 全部
    - 传单个 symbol → 单资产快速版（旧 run_single 等效）
    - 多资产并行：macro 共享 1 次，每个资产独立线程跑（内部 Round 1/2 也并行）
    - max_debate_rounds 默认 4 真讨论；旧 daily_report cron 走单独路径不受影响
    """
    task_id = uuid.uuid4().hex[:12]
    started_at = _now_iso()
    _write_committee_status(task_id, {
        "task_id": task_id,
        "status": "queued",
        "phase": "queued",
        "started_at": started_at,
        "note": body.note,
        "symbols": body.symbols,
        "max_debate_rounds": body.max_debate_rounds,
        "events": [],
    })

    # 在独立 daemon 线程跑同步委员会任务，不占用 uvicorn 事件循环（修复 #105 假死）。
    # 任务只通过 status.json 文件回报进度，客户端轮询 poll_url 获取结果，无需把结果
    # 桥回事件循环——这正是能绕开 anyio TestClient 不兼容的原因（见 _run_committee_task）。
    threading.Thread(
        target=_run_committee_task,
        args=(task_id,),
        kwargs={
            "symbols": body.symbols,
            "max_rounds": body.max_debate_rounds,
            "event_ids": body.event_ids,
        },
        daemon=True,
        name=f"committee-{task_id}",
    ).start()

    return CommitteeRunResponse(
        task_id=task_id,
        status="queued",
        started_at=started_at,
        poll_url=f"/api/committee/{task_id}",
    )


@router.get("/api/committee/{task_id}", response_model=CommitteeStatusResponse, tags=["committee"])
async def committee_status(task_id: str) -> CommitteeStatusResponse:
    """查询委员会任务状态（pending → running → done/error）"""
    status = _read_committee_status(task_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"task_id {task_id} 不存在")
    # 兜底未写入字段，避免 Pydantic validation 失败
    status.setdefault("task_id", task_id)
    status.setdefault("started_at", _now_iso())
    return CommitteeStatusResponse(**status)


@router.get("/api/committee/{task_id}/audit", tags=["committee"])
async def committee_audit_meta(task_id: str) -> Dict[str, Any]:
    """读取审计 trail（commit_hash / model / temperature / max_debate_rounds 等）

    给合规 / 复盘用：监管来查"那天 verdict 是哪个 commit / 哪个 model 跑的"时一查就有。
    Frontmatter 进 memory/.committee/<task_id>/meta.json，永不被 progress 覆盖。
    """
    meta_path = _committee_meta_path(task_id)
    if not meta_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"audit meta for task_id {task_id} not found（旧 task 没写过 meta.json）",
        )
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"meta.json 损坏: {e}") from e


@router.get("/api/committee/live/{task_id}", tags=["committee"])
async def committee_live(task_id: str) -> StreamingResponse:
    """SSE 流式推送 task 状态变化（前端 EventSource 订阅）

    用法（前端）：
        const es = new EventSource(`/api/committee/live/${taskId}`);
        es.addEventListener('progress', (e) => { ... });
        es.addEventListener('done', () => es.close());

    每 2s 重读 status.json 如有变化推送 progress event；
    每 25s 推送 `: keepalive` 注释防 CF Access 5 min idle 超时
    """
    async def event_stream():
        last_status_str: Optional[str] = None
        max_iterations = 600   # 600 * 2s = 20 min 上限
        keepalive_every = 12   # 每 12 次循环（24s）发一次心跳
        loop_count = 0

        while loop_count < max_iterations:
            status = _read_committee_status(task_id)
            if status is None:
                yield f"event: not_found\ndata: {json.dumps({'task_id': task_id})}\n\n"
                return

            current = json.dumps(status, ensure_ascii=False, default=str)
            if current != last_status_str:
                last_status_str = current
                yield f"event: progress\ndata: {current}\n\n"

            phase = status.get("status")
            if phase == "done":
                yield f"event: done\ndata: {current}\n\n"
                return
            if phase == "error":
                yield f"event: error\ndata: {current}\n\n"
                return

            loop_count += 1
            if loop_count % keepalive_every == 0:
                yield ": keepalive\n\n"   # SSE 注释行，防 CF idle timeout
            await asyncio.sleep(2)

        yield f"event: timeout\ndata: {json.dumps({'task_id': task_id, 'reason': 'exceeded_20min'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",   # 关 nginx/Caddy 缓冲
            "Connection": "keep-alive",
        },
    )


@router.post("/api/committee/prepare", tags=["committee"])
async def committee_prepare(body: CommitteePrepareRequest = Body(...)) -> Dict[str, Any]:
    """Coordinator 路径的 prep RPC：cmd_prepare_committee 同款自包含 brief

    返回 brief + 6 段角色 prompt 全内联——远端客户端的 Claude 据此 spawn 4 个
    subagent，全程不需要本地 memory/。symbol 不在 target_assets 时返回 CLI
    同款 status=error dict（200）。

    注意：内部拉 2y 行情 + 情绪/估值事实块，同步阻塞数十秒（与既有委员会端点
    同款已知限制，生产建议 --workers 2+）。
    """
    from openinvest.core.committee_runner import prepare_committee_brief
    try:
        return prepare_committee_brief(body.symbol)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"openInvest 还没初始化（{exc!s}）。先在 hub 上跑 "
                "`~/.claude/skills/invest/scripts/run.sh init` 完成 onboarding。"
            ),
        ) from exc


@router.post("/api/committee/save", tags=["committee"])
async def committee_save(body: CommitteeSaveRequest = Body(...)) -> Dict[str, Any]:
    """Coordinator 路径的 persist RPC：cmd_save_committee 同款落盘

    解析 transcript → parse_cio_memo（含确定性防御降级）→ 落
    memory/.committee/<date>/<sym>.md + dream_event，返回 {saved, verdict}。
    """
    if not body.transcript.strip():
        raise HTTPException(status_code=400, detail="transcript 为空")
    from openinvest.core.committee_runner import save_committee_transcript
    return save_committee_transcript(body.symbol, body.transcript)
