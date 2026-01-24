import imaplib
import email
import re
import os
import datetime
from email.header import decode_header
from typing import List, Dict, Optional

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
            print(f"❌ IMAP Connection failed: {e}")
            return False

    def close(self):
        try:
            self.mail.close()
            self.mail.logout()
        except:
            pass

    def fetch_trade_confirmations(self, lookback_days=180, processed_ids=None) -> List[Dict]:
        if processed_ids is None:
            processed_ids = []

        self.mail.select("inbox")

        date_since = (datetime.date.today() - datetime.timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        search_criteria = f'(FROM "commsec.com.au" SINCE "{date_since}")'
        
        status, messages = self.mail.search(None, search_criteria)
        if status != "OK" or not messages[0]:
            print(f"⚠️ No CommSec emails found since {date_since}")
            return []

        trades = []
        email_ids = messages[0].split()

        print(f"🔎 Found {len(email_ids)} emails from CommSec. Scanning for trades...")

        for e_id in email_ids:
            e_id_str = e_id.decode()
            if e_id_str in processed_ids:
                continue

            try:
                # 获取邮件内容
                _, msg_data = self.mail.fetch(e_id, "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        subject = self._get_subject(msg)
                        body = self._get_body(msg)
                        
                        # 传入 subject 和 body 一起尝试解析
                        trade_data = self._parse_commsec_body(body, subject)
                        
                        if trade_data:
                            trade_data['email_id'] = e_id_str
                            trade_data['date_processed'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            trades.append(trade_data)
                            print(f"✅ Found Trade: {trade_data['action']} {trade_data['units']} {trade_data['symbol']}")
            except Exception as e:
                print(f"⚠️ Error parsing email {e_id_str}: {e}")

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