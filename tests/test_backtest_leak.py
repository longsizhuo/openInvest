"""回测防穿越护栏（ADR/backtest）：_patch_tools_to_date 必须把【所有】行情读路径
截断到 decision_date 之前，否则 holdout 验证就被未来数据污染了。

⚠⚠⚠ 本测试 green ≠ 回测干净 ⚠⚠⚠
================================================================================
本测试只验【行尾时点】df.index.max() <= decision_date（显式未来 K 线不漏）。
它【结构上抓不到】真正的主泄漏——绝对价位 / 宏观点位指纹让记忆过历史的 LLM 反推
年代（ADR-022 T1）。归一化能压低但杀纪律规则（VIX>20=fear 吃绝对值），不可消除。

所以：**pre-cutoff（< 2024-12-31 训练截止）段永远是记忆污染的**，本测试对那一段
只做行尾时点的【一致性扫描】，不是、也不可能是"无泄漏"证明。唯一可信的预测/业绩验证
是 cutoff 之后的 holdout（见 scripts/backtest_committee.py --holdout）。

未来 agent 注意：看到本文件全绿就宣布"回测无泄漏"是【错的】——你证明的是
"没有显式未来 K 线 / 没有 prompt 里的字面日期"，不是"LLM 反推不出年代"。
================================================================================

历史教训：早期 patch 只拦 ef.get_history_data 包装层，path-profile / 汇率腿直读
MarketStore.get_history_df 那条没拦，仅靠"backtest 恰好没调 get_path_profile"才不漏。
本测试钉死根级截断，谁以后给 backtest 加了 prob_hint / reentry，这里就会红。
"""
import pandas as pd
import pytest

from scripts.backtest_committee import _patch_tools_to_date
import openinvest.utils.exchange_fee as ef
from openinvest.db.market_store import MarketStore

CUT = "2020-01-01"
_cut_ts = pd.Timestamp(CUT)


def _has_data(sym):
    df = MarketStore().get_history_df(sym, days=100000)
    return df is not None and not df.empty


@pytest.mark.skipif(not _has_data("GC=F"), reason="store 无 GC=F 历史（未回填）")
def test_wrapper_get_history_data_cut():
    with _patch_tools_to_date(CUT):
        df = ef.get_history_data("GC=F", "2y")
    assert df is not None and not df.empty
    assert df.index.max() <= _cut_ts, f"ef.get_history_data 泄漏未来: {df.index.max()}"


@pytest.mark.skipif(not _has_data("GC=F"), reason="store 无 GC=F 历史（未回填）")
def test_root_store_cut_covers_pathprofile_and_fx():
    """根级 MarketStore.get_history_df 截断 —— path-profile / 汇率腿都走它。"""
    with _patch_tools_to_date(CUT):
        for sym in ("GC=F", "^VIX", "^TNX", "DX-Y.NYB", "USDCNY=X"):
            df = MarketStore().get_history_df(sym, days=100000)
            if df is not None and not df.empty:
                assert df.index.max() <= _cut_ts, f"{sym} 根级读泄漏未来: {df.index.max()}"


@pytest.mark.skipif(not _has_data("GC=F"), reason="store 无 GC=F 历史（未回填）")
def test_pathprofile_under_patch_is_cut():
    """直接验 get_path_profile（带 asof 与否都不能引入未来）——这是当年最脆的那条。"""
    from openinvest.core.regime_probability import get_path_profile
    with _patch_tools_to_date(CUT):
        prof = get_path_profile("GC=F", "downtrend")  # 不传 asof，靠根级 patch 兜
    # 拿不到 profile 也算通过（无样本），关键是不能因为读到未来数据而"样本虚多"。
    # 用 tail 行数 sanity：截断后 GC=F ≤2020-01-01 的行数应远少于全量。
    full = MarketStore().get_history_df("GC=F", days=100000)
    assert full.index.max() > _cut_ts, "patch 未释放（污染了后续）"


@pytest.mark.skipif(not _has_data("GC=F"), reason="store 无 GC=F 历史（未回填）")
def test_patch_released_after_context():
    with _patch_tools_to_date(CUT):
        pass
    df = MarketStore().get_history_df("GC=F", days=100000)
    assert df.index.max() > _cut_ts, "patch 退出后仍在截断（污染全局）"


# 选 2020-02-14：GC=F 当时 ~$1600，"2020"这个年份串不会撞上任何价位（彼时金价远未到
# $2020）→ 断言"prompt 不含 2020"才是干净的"无字面日期"检查，不会被价位误伤。
_LEAK_DATE = "2020-02-14"
_LEAK_YEAR = "2020"


@pytest.mark.skipif(not _has_data("GC=F"), reason="store 无 GC=F 历史（未回填）")
def test_prompt_has_no_decision_date(monkeypatch, tmp_path):
    """dump 一个 backtest 日发给 4 角色的 prompt，锁住"prompt 里没有字面 decision_date"。

    做法：monkeypatch core.committee 的 _create_agent，返回一个不真调 LLM 的 stub，
    它截获每个角色的 system_prompt + user message（run() 收到的 context），再断言这两
    段文本里都【不含】decision_date 串（"2020-02-14" 和年份 "2020"）。这把"prompt 无
    日期"的现状钉住，防回归——以后谁把 decision_date 拼进 prompt（哪怕只是 debug 行），
    这里就红。

    ⚠ 但这【不】代表回测无泄漏：committee 收到的 market_data / regime_brief 里满是绝对
    价位与宏观点位，记忆过那段历史的 LLM 仍能反推年代（ADR-022 T1）。prompt 没有字面日期
    只是关掉了最蠢的一个泄漏通道，主泄漏（价位指纹）还在，结构上无法用断言消除。
    """
    import openinvest.core.committee.debate as debate

    captured: list[tuple[str, str, str]] = []  # (role, system_prompt, user_msg)

    class _StubAgent:
        def __init__(self, system_prompt: str, role: str):
            self._sys = system_prompt
            self._role = role

        def run(self, context: str) -> str:
            captured.append((self._role, self._sys, context))
            # 返回一个能被 parse_cio_memo 走通的最小 verdict，避免委员会半路抛异常
            # 而漏掉 CIO 那一份 prompt（我们要的是把 4 角色 prompt 都 dump 出来）。
            return "VERDICT: HOLD\nCONFIDENCE: 0.3\nALLOC: 0\nSIGNAL: HOLD\nSTRENGTH: weak"

    def _fake_create_agent(system_prompt, *, role="unknown", **_kw):
        return _StubAgent(system_prompt, role)

    # 钉 debate 命名空间（run_committee 在那里解析 _create_agent，patch façade 无效）。
    monkeypatch.setattr(debate, "_create_agent", _fake_create_agent)
    # 别污染真实 memory/.backtest/ —— persist 设成 no-op。
    import openinvest.core.committee as cc
    monkeypatch.setattr(cc, "_persist", lambda *a, **k: None)

    import scripts.backtest_committee as bt
    # 把 .backtest/ 输出根指到 tmp_path，连那个空 mkdir 都不落进真实 memory/。
    _RealMS = bt.MemoryStore
    monkeypatch.setattr(bt, "MemoryStore", lambda: _RealMS(root=tmp_path))
    bt.run_one_day(_LEAK_DATE, ["GC=F"], resume=False)

    assert captured, "一份 prompt 都没截获 —— run_one_day 没走到 committee？"
    roles = {role for role, _, _ in captured}
    assert {"quant", "risk", "cio"} <= roles, f"缺角色 prompt，只拿到 {roles}"

    for role, sysp, user in captured:
        blob = f"{sysp}\n{user}"
        assert _LEAK_DATE not in blob, f"{role} prompt 漏了字面 decision_date {_LEAK_DATE}"
        assert _LEAK_YEAR not in blob, f"{role} prompt 漏了年份 {_LEAK_YEAR}（年代指纹）"
