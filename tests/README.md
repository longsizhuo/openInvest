# tests/

pytest 测试套件。`conftest.py` 把仓库根加进 `sys.path`，无需安装即可 `import core/jobs/scripts`。

## 内容

- `conftest.py` — pytest 配置（仅加 sys.path）
- `test_memory_store.py` — 数据完整性核心：atomic write / transaction RMW / 50 线程并发不丢更新
- `test_schemas.py` — Pydantic v2 schema 校验（v2 含 cash dict + holdings list + Holding 枚举）
- `test_web_api.py` — FastAPI 全端点 TestClient 跑通（GET 读、POST 写、DELETE，schema rollback 验证）
- `test_committee_parser.py` — 委员会 LLM 输出解析（verdict / confidence 提取）
- `test_commsec.py` — CommSec 邮件解析（mock IMAP）
- `test_gold_price.py` — 金价快照 + DB 兜底逻辑
- `test_pnl_snapshot.py` — PnL 计算 + SVG 渲染

## 跑法

```bash
uv run pytest                    # 全量
uv run pytest tests/test_xxx.py  # 单文件
uv run pytest -v -k "concurrent" # 关键词过滤
```

## 与其他目录的关系

- 测真实代码：`core/` `connectors/` `jobs/` `utils/`
- 不测 `services/` 的网络打外（用 mock）
