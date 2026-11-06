"""Synthetic anchor fixture for old research transforms; not a live or historical prompt."""
def anchored_control(asset, json_mode=False):
    from scripts import backtest_committee as bc
    return "SYNTHETIC CONTROL\n" + bc._CIO_T2_ANCHOR + "\n" + bc._CIO_085_RULE + "\n" + bc._CIO_RULE2 + "\nVERDICT: HOLD\n"
