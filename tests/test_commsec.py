import unittest
import os
import sys
from dotenv import load_dotenv

# 添加项目根目录到 path 以便导入
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.commsec_reader import CommSecReader

class TestCommSecParsing(unittest.TestCase):
    def setUp(self):
        # 只需要实例化，不需要真实连接
        self.reader = CommSecReader("test", "test")

    def test_parse_full_body_standard(self):
        """测试标准的 CommSec 邮件正文格式"""
        text = """
        Hi Sizhuo,

        You've bought 17 units in BETASHARES NASDAQ 100 ETF (NDQ) at a price of $55.72 per unit (not including brokerage), on trading account ****914 MR SIZHUO LONG.

        The total settlement amount, including brokerage, is $952.23. We'll debit this from your settlement account on 07 Jan 2026.
        """
        result = self.reader._parse_commsec_body(text)
        
        self.assertIsNotNone(result, "Should parse standard body")
        self.assertEqual(result['action'], 'bought')
        self.assertEqual(result['units'], 17.0)
        self.assertEqual(result['symbol'], 'NDQ.AX')
        self.assertEqual(result['price_per_unit'], 55.72)
        self.assertEqual(result['total_amount'], 952.23)

    def test_parse_simple_header_style(self):
        """测试简短的标题/摘要格式"""
        text = "CommSec - Bought 54 units of NDQ"
        result = self.reader._parse_commsec_body(text)
        
        self.assertIsNotNone(result, "Should parse simple header style")
        self.assertEqual(result['action'], 'bought')
        self.assertEqual(result['units'], 54.0)
        self.assertEqual(result['symbol'], 'NDQ.AX')
        # 简短格式通常没有价格信息，期望为 0.0
        self.assertEqual(result['price_per_unit'], 0.0) 

    def test_parse_sold_format(self):
        """测试卖出格式"""
        text = "You've sold 10 units in BHP GROUP LIMITED (BHP) at a price of $45.00"
        result = self.reader._parse_commsec_body(text)
        
        self.assertEqual(result['action'], 'sold')
        self.assertEqual(result['units'], 10.0)
        self.assertEqual(result['symbol'], 'BHP.AX')
        self.assertEqual(result['price_per_unit'], 45.00)

def run_integration_test():
    """尝试真实连接 IMAP 并读取"""
    load_dotenv()
    user = os.getenv("EMAIL_SENDER")
    pw = os.getenv("EMAIL_PASSWORD")
    
    if not user or not pw:
        print("⚠️ Skipping integration test: EMAIL_SENDER or EMAIL_PASSWORD not set in .env")
        return

    print(f"\n🔌 Connecting to Gmail as {user}...")
    reader = CommSecReader(user, pw)
    if reader.connect():
        print("✅ IMAP Connection successful!")
        print("🔎 Searching for CommSec emails (last 180 days)...")
        
        # 为了调试，我们需要手动调用内部逻辑来查看每封邮件
        import datetime
        import email
        
        reader.mail.select("inbox")
        date_since = (datetime.date.today() - datetime.timedelta(days=180)).strftime("%d-%b-%Y")
        search_criteria = f'(FROM "commsec.com.au" SINCE "{date_since}")'
        status, messages = reader.mail.search(None, search_criteria)
        
        email_ids = messages[0].split()
        print(f"🔎 Found {len(email_ids)} raw emails. Inspecting headers...")

        for e_id in email_ids:
            try:
                _, msg_data = reader.mail.fetch(e_id, "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        subject = reader._get_subject(msg)
                        body = reader._get_body(msg)
                        
                        print(f"\n📧 [ID: {e_id.decode()}] Subject: {subject}")
                        # print(f"   Body Preview: {body[:100].replace(chr(10), ' ')}...")
                        
                        parsed = reader._parse_commsec_body(body, subject)
                        if parsed:
                            print(f"   ✅ PARSED: {parsed['action']} {parsed['units']} {parsed['symbol']}")
                        else:
                            print(f"   ❌ Failed to parse.")
                            # 如果没解析出来，可能是正文提取问题，打印更多
                            # print(f"   --- Body Dump ---\n{body[:200]}\n   -----------------")
            except Exception as e:
                print(f"Error: {e}")

        reader.close()
    else:
        print("❌ Connection failed. Check your password or network.")

if __name__ == '__main__':
    # 1. 运行单元测试
    # print("🧪 Running Unit Tests (Parsing Logic)...")
    # suite = unittest.TestLoader().loadTestsFromTestCase(TestCommSecParsing)
    # result = unittest.TextTestRunner(verbosity=1).run(suite)
    
    # 2. 如果单元测试通过，运行集成测试
    # if result.wasSuccessful():
        print("\n" + "="*50)
        print("🚀 Running Integration Test (Debug Mode)")
        print("="*50)
        run_integration_test()