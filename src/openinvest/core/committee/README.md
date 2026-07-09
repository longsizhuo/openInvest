# core/committee/ — 投资委员会编排（包）

原扁平 `core/committee.py` 按职责拆成本包 + 薄壳 `__init__.py` façade（#57 拆包，沿用 #56 playbook）。
`from core.committee import X` 与 `core.committee.X` 属性访问对所有历史符号仍有效，entry / service / 测试 / 脚本零改。

| 子模块 | 职责 |
|---|---|
| `agent_io.py` | SDKAgent 工厂 `_create_agent` + 重试 `_ask` / 并行 `_parallel_ask` + LLM 重试常量 + 失败哨兵 |
| `cio_parse.py` | CIO memo 解析 `parse_cio_memo`（6 道 sanity check）+ 各类正则 + THRESHOLDS + 集中度覆写 |
| `views.py` | 跨资产共享评估 `run_macro_view` |
| `debate.py` | 主流程 `run_committee` + `CommitteeReport` + 收敛判定 + 辩论历史拼装 |
| `persist.py` | 决议落盘 `_persist` + macro 快照 `_capture_macro_context` |

依赖方向（import 顺序）：agent_io → cio_parse → persist → views → loaders → debate。
注意：`run_committee` 在 debate.py 命名空间内解析 `_create_agent` / `_persist`，测试/脚本 monkeypatch 钉的是 `core.committee.debate.*`，不是 façade 属性。
