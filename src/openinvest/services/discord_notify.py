"""Discord 报警通道：把事件/verdict 报警实时推到用户与 AI 助手共用的 DM 频道。

设计（2026-07-14）：
- invest 侧**不直接持有 Discord token**——token 已经在 ChatBot / Hermes 两处
  维护，rotate 不同步咬过一次（chat-bot 崩溃循环一天半）。invest 只握内网
  共享密钥，POST 给同宿主机 ChatBot 的 alert server（/alert/invest），由它
  代发 DM。
- 推到 DM 的价值不止"实时"：那个 DM 频道同时是 Hermes agent 会话，用户
  收到报警后直接回复即可就地追问/让 agent 跑委员会——投递零 LLM token，
  智能按需触发。
- Best-effort：任何失败只打 log 返回 False，绝不抛异常——邮件仍是保底
  归档通道，Discord 挂了不能影响 event_watch 主流程。
- 未配置 CHATBOT_ALERT_URL / CHATBOT_INTERNAL_KEY 时静默跳过（fork 用户
  没有这套基础设施，行为与从前完全一致）。
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

# Discord 单条消息上限 2000，留余量给转发侧包装
_MAX_LEN = 1900


def send_discord_alert(text: str) -> bool:
    """把报警文本推给 ChatBot 转发到 Discord DM；成功返回 True。"""
    url = os.getenv("CHATBOT_ALERT_URL", "")
    key = os.getenv("CHATBOT_INTERNAL_KEY", "")
    if not url or not key or not text:
        return False
    try:
        resp = requests.post(
            url,
            json={"type": "invest_event", "text": text[:_MAX_LEN]},
            headers={"X-Internal-Key": key},
            timeout=5,
        )
        if resp.status_code != 200:
            log.warning(
                "discord alert failed: HTTP %s %s", resp.status_code, resp.text[:120]
            )
            return False
        return True
    except Exception as e:  # noqa: BLE001 —— best-effort 通道，任何异常都不上抛
        log.warning("discord alert failed: %s: %s", type(e).__name__, e)
        return False
