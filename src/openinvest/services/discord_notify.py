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
import hashlib

import requests

log = logging.getLogger(__name__)

# Discord 单条消息上限 2000，留余量给转发侧包装
_MAX_LEN = 1900


def lookup_discord_alert_receipt(notification_id: str, text: str) -> dict:
    """Read-only recovery; missing or ambiguous receipt never authorizes resend."""
    url=os.getenv("CHATBOT_ALERT_URL","");key=os.getenv("CHATBOT_INTERNAL_KEY","")
    if not url or not key or not notification_id:
        return {"accepted":False,"status":"unknown","reason":"receipt_lookup_unavailable"}
    try:
        response=requests.get(url.rstrip("/")+"/"+notification_id,headers={"X-Internal-Key":key},timeout=5)
        body=response.json()
        if (response.status_code==200 and isinstance(body,dict) and body.get("ok") is True
                and body.get("delivery_status")=="accepted" and body.get("notification_id")==notification_id
                and body.get("text_sha256")==hashlib.sha256(text[:_MAX_LEN].strip().encode()).hexdigest()
                and str(body.get("message_id") or "").isdecimal()):
            return {"accepted":True,"status":"accepted","target":str(body["message_id"]),
                    "accepted_at":body.get("accepted_at"),"recipient_read_receipt":"unknown"}
    except Exception:
        pass
    return {"accepted":False,"status":"unknown","reason":"receipt_not_confirmed"}


def send_discord_alert(text: str) -> bool:
    """兼容布尔接口：只有提供 Discord 消息编号的回执才算通道接受。"""
    return send_discord_alert_receipt(text)["accepted"]


def send_discord_alert_receipt(text: str, *, notification_id: str | None = None) -> dict:
    url = os.getenv("CHATBOT_ALERT_URL", "")
    key = os.getenv("CHATBOT_INTERNAL_KEY", "")
    if not url or not key or not text:
        return {"accepted":False,"status":"unavailable","reason":"not_configured"}
    payload={"type":"invest_event","text":text[:_MAX_LEN]}
    if notification_id:
        payload["notification_id"]=notification_id
    try:
        response=requests.post(url,json=payload,headers={"X-Internal-Key":key},timeout=5)
        try:
            body=response.json()
        except ValueError:
            body={}
        if not isinstance(body,dict):
            body={}
        if body.get("delivery_status")=="not_configured" or body.get("msg")=="dm user not configured":
            return {"accepted":False,"status":"unavailable","reason":"recipient_not_configured"}
        message_id=str(body.get("message_id") or "")
        if (response.status_code==200 and body.get("ok") is True and body.get("delivery_status")=="accepted"
                and message_id.isdecimal() and (not notification_id or body.get("notification_id")==notification_id)):
            return {"accepted":True,"status":"accepted","target":message_id,"recipient_read_receipt":"unknown"}
        state="failed" if body.get("delivery_status")=="failed" else "unknown"
        return {"accepted":False,"status":state,"reason":"provider_acceptance_not_confirmed"}
    except requests.ConnectTimeout:
        return {"accepted":False,"status":"failed","reason":"relay_connect_timeout_before_send"}
    except requests.Timeout:
        return lookup_discord_alert_receipt(notification_id,text) if notification_id else {"accepted":False,"status":"unknown","reason":"relay_timeout"}
    except requests.ConnectionError:
        return lookup_discord_alert_receipt(notification_id,text) if notification_id else {"accepted":False,"status":"unknown","reason":"relay_connection_interrupted"}
    except Exception as error:
        log.warning("discord alert failed: %s",type(error).__name__)
        return {"accepted":False,"status":"unknown","reason":type(error).__name__}
