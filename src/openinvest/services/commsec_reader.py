import imaplib
import email
import logging
import re
import os
import datetime
from email.header import decode_header
from typing import List, Dict, Optional

log = logging.getLogger(__name__)

class CommSecReader:
    def __init__(self, email_user: str, email_pass: str):
        self.email_user = email_user
        self.email_pass = email_pass
        self.imap_server = "imap.gmail.com"

    def connect(self):
        try:
            self.mail = imaplib.IMAP4_SSL(self.imap_server)
            self.mail.login(self.email_user, self.email_pass)
            return True
        except Exception as e:
            log.error("IMAP Connection failed: %s", e)
            return False

    def close(self):
        try:
            self.mail.close()
            self.mail.logout()
        except:
            pass

    def fetch_trade_confirmations(self, lookback_days=180, processed_ids=None) -> List[Dict]:
        # 2026-10-07 #231：去重键从 IMAP 序号改为 Message-ID（缺失退化 UIDVALIDITY:UID）。
        # 序号是"当前第几封"，任何 expunge（Gmail 归档/删信即 INBOX expunge）都会让后面的
        # 邮件整体前移 → 已记账的邮件换了新序号被二次记账，新邮件撞上旧序号被静默跳过。
        # 新键带 msgid:/uid: 前缀，与旧的纯数字序号键不可能相撞。
        processed = {str(k) for k in processed_ids or []}
        # #231 前写入的旧键是纯数字序号：仍按旧语义拿当前序号比对跳过（不比旧代码差），
        # 否则升级后 lookback 内所有已记账邮件换新键重新入账 = 批量双记。
        legacy_seqs = {k for k in processed if k.isdigit()}
        if legacy_seqs:
            log.warning(
                "processed_emails 含 %d 条 #231 前的 IMAP 序号旧键，仍按序号跳过（会随 expunge "
                "漂移）；确认对应邮件都已过 lookback 窗口后可删掉这些纯数字键", len(legacy_seqs),
            )

        self.mail.select("inbox")
        uidvalidity = ((self.mail.response("UIDVALIDITY")[1] or [None])[0] or b"").decode()

        date_since = (datetime.date.today() - datetime.timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        search_criteria = f'(FROM "commsec.com.au" SINCE "{date_since}")'
        
        status, messages = self.mail.uid("SEARCH", None, search_criteria)
        if status != "OK" or not messages[0]:
            log.warning("No CommSec emails found since %s", date_since)
            return []

        trades = []
        uids = messages[0].split()

        log.info("Found %d emails from CommSec. Scanning for trades...", len(uids))

        for uid in uids:
            uid_str = uid.decode()
            try:
                # 获取邮件内容（UID FETCH：UID 不受 expunge 重编号影响）。
                # ponytail: 已处理的邮件也整封拉下来再按 Message-ID 跳过；CommSec 邮件小、
                # 量少，嫌流量再改成先 BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)] 批量取头
                _, msg_data = self.mail.uid("FETCH", uid, "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        message_id = str(msg.get("Message-ID") or "").strip()
                        email_id = f"msgid:{message_id}" if message_id else f"uid:{uidvalidity}:{uid_str}"
                        if email_id in processed:
                            continue
                        # FETCH 响应首个 token 是该邮件当前序号（仅供旧键比对）
                        if legacy_seqs and response_part[0].split()[0].decode() in legacy_seqs:
                            continue

                        subject = self._get_subject(msg)
                        body = self._get_body(msg)
                        
                        # 传入 subject 和 body 一起尝试解析
                        trade_data = self._parse_commsec_body(body, subject)
                        
                        if trade_data:
                            trade_data['email_id'] = email_id
                            trade_data['date_processed'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            trades.append(trade_data)
                            log.info("Found Trade: %s %s %s", trade_data['action'], trade_data['units'], trade_data['symbol'])
            except Exception as e:
                log.warning("Error parsing email uid=%s: %s", uid_str, e)

        return trades

    def _get_subject(self, msg):
        subject, encoding = decode_header(msg["Subject"])[0]
        if isinstance(subject, bytes):
            subject = subject.decode(encoding if encoding else "utf-8")
        return subject

    def _get_body(self, msg):
        body_text = ""
        body_html = ""

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                
                if "attachment" in content_disposition:
                    continue

                if content_type == "text/plain":
                    try:
                        body_text = part.get_payload(decode=True).decode(errors='ignore')
                    except: pass
                elif content_type == "text/html":
                    try:
                        body_html = part.get_payload(decode=True).decode(errors='ignore')
                    except: pass
        else:
            try:
                payload = msg.get_payload(decode=True).decode(errors='ignore')
                if msg.get_content_type() == "text/html":
                    body_html = payload
                else:
                    body_text = payload
            except: pass

        if body_text.strip():
            return body_text
        
        if body_html.strip():
            # 简单的移除HTML标签
            return re.sub(r'<[^>]+>', ' ', body_html)
            
        return ""

    def _parse_commsec_body(self, body: str, subject: str = "") -> Optional[Dict]:
        """
        解析邮件正文 + 标题。
        """
        # 合并内容，清理空格
        clean_body = re.sub(r'\s+', ' ', f"{subject} {body}").strip()

        # Regex 1: Full sentence
        pattern_full = r"(?:You've|You)\s+(bought|sold)\s+([\d,]+)\s+units\s+in\s+.*?\s*\((\w+)\)\s+at\s+a\s+price\s+of\s+\$([\d.]+)"
        match = re.search(pattern_full, clean_body, re.IGNORECASE)

        action, units, symbol, price = None, 0, "", 0.0

        if match:
            action = match.group(1).lower()
            units = float(match.group(2).replace(',', ''))
            symbol = match.group(3).upper()
            price = float(match.group(4))
        else:
            # Regex 2: Simple "Bought 54 units of NDQ"
            # 这种格式通常在 Subject 里
            pattern_simple = r"(bought|sold)\s+([\d,]+)\s+units\s+of\s+(\w+)"
            match_simple = re.search(pattern_simple, clean_body, re.IGNORECASE)
            
            if match_simple:
                action = match_simple.group(1).lower()
                units = float(match_simple.group(2).replace(',', ''))
                symbol = match_simple.group(3).upper()
                # price 暂无
        
        if not action:
            return None

        # 尝试提取总成本
        total_cost = 0.0
        pattern_total = r"total settlement amount.*? is \$([\d,.]+)"
        match_total = re.search(pattern_total, clean_body, re.IGNORECASE)
        if match_total:
            cost_str = match_total.group(1).replace(',', '').rstrip('.')
            total_cost = float(cost_str)
        
        # 补全单价
        if price == 0 and units > 0 and total_cost > 0:
            price = round(total_cost / units, 2)

        if not symbol.endswith(".AX"):
            symbol = f"{symbol}.AX"

        return {
            "action": action, 
            "units": units,
            "symbol": symbol,
            "price_per_unit": price,
            "total_amount": total_cost,
            "currency": "AUD"
        }