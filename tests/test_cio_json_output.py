"""P1 — CIO JSON Output 路径：parse_cio_memo(fields=...) 与 regex 同口径，
负 alloc 保号（治 TRIM 被吞那类 regex bug），sanity checks 照旧生效，
worker_brief backstop 在 JSON 模式（CIO 输出不回显哨兵）下仍能强制 HOLD。
+ supports_json_output 门控（默认尝试 + env 优雅覆盖，非 provider==）。"""
import os

import pytest

from core.committee.cio_parse import parse_cio_memo
from utils.llm import supports_json_output


def test_fields_basic_same_shape_as_regex():
    fields = {"verdict": "accumulate", "confidence": 0.7, "dominant_view": "quant",
              "suggested_alloc_cny": 5000, "trim_reason": None}
    r = parse_cio_memo("(json mode raw)", fields=fields)
    assert r["verdict"] == "ACCUMULATE"      # 大写归一
    assert r["confidence"] == 0.7
    assert r["dominant_view"] == "quant"
    assert r["alloc_cny"] == 5000


def test_fields_preserve_negative_alloc():
    # P1 的核心价值：JSON 保住负号，不再靠 regex（曾把 TRIM 负 alloc 吞掉）
    fields = {"verdict": "TRIM", "confidence": 0.6, "suggested_alloc_cny": -5000,
              "trim_reason": "stop_loss", "reentry_price": 950,
              "reentry_condition": "跌破 ¥950", "expected_path": "回踩后再加"}
    r = parse_cio_memo("(json)", fields=fields, current_price=1000.0)
    assert r["verdict"] == "TRIM"
    assert r["alloc_cny"] == -5000
    assert r["reentry_price"] == 950.0


def test_fields_sanity_buy_overdrive_downgrade():
    # Sanity 1 仍生效：高置信 BUY → 降级 ACCUMULATE
    r = parse_cio_memo("(json)", fields={"verdict": "BUY", "confidence": 0.99,
                                         "suggested_alloc_cny": 3000})
    assert r["verdict"] == "ACCUMULATE"
    assert r["_original_verdict"] == "BUY"


def test_fields_worker_unavailable_backstop_via_brief():
    # JSON 模式 CIO 输出是纯 JSON、不回显哨兵 → 靠 worker_brief 查到 worker 失败 → 强制 HOLD
    fields = {"verdict": "BUY", "confidence": 0.9, "suggested_alloc_cny": 8000}
    brief = "QUANT: ...\nRISK: [WORKER_UNAVAILABLE] reason=retry_exhausted\n"
    r = parse_cio_memo("(json, 无哨兵)", fields=fields, worker_brief=brief)
    assert r["verdict"] == "HOLD"
    assert r["alloc_cny"] == 0


def test_fields_garbage_degrades_not_crash():
    # 字段类型异常 → 退化到默认值，不抛
    r = parse_cio_memo("x", fields={"verdict": None, "confidence": "abc",
                                    "suggested_alloc_cny": "not a number"})
    assert r["verdict"] == "UNCLEAR"
    assert r["confidence"] == 0.0
    assert r["alloc_cny"] == 0


def test_supports_json_output_default_and_override():
    old = os.environ.pop("INVEST_FORCE_JSON_OUTPUT", None)
    try:
        assert supports_json_output() is True          # 默认尝试（非 provider==）
        os.environ["INVEST_FORCE_JSON_OUTPUT"] = "0"
        assert supports_json_output() is False          # 优雅关
        os.environ["INVEST_FORCE_JSON_OUTPUT"] = "1"
        assert supports_json_output() is True
    finally:
        os.environ.pop("INVEST_FORCE_JSON_OUTPUT", None)
        if old is not None:
            os.environ["INVEST_FORCE_JSON_OUTPUT"] = old


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
