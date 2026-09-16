import custom_strategy


def test_analyze_sets_last_signal_and_score_gap(monkeypatch):
    """Ensure analyze_custom_strategy stores the last signal and score gap."""

    def fake_run_strategy(strategy):
        report = "REPORT"
        signals = [{
            "pair": "USD_JPY",
            "action": "BUY",
            "entry": 111.0,
            "stop_loss": 110.0,
            "take_profit": 112.0,
            "risk_reward": 1.0,
            "reasoning": "test",
        }]
        score_gap = 1.23
        return report, signals, score_gap

    monkeypatch.setattr(custom_strategy, "run_strategy", fake_run_strategy)

    report = custom_strategy.analyze_custom_strategy()
    last = custom_strategy.get_last_signal()
    assert last is not None
    assert last["pair"] == "USD_JPY"
    assert custom_strategy.get_last_score_gap() == 1.23


def test_dominance_guard_flag(monkeypatch):
    """When the strategy requests a dominance guard, the flag should be set."""

    def fake_run_strategy_set_flag(strategy):
        # emulate strategy logic setting the module flag
        custom_strategy._last_dominance_guard_triggered = True
        return "R", [], 0.0

    monkeypatch.setattr(custom_strategy, "run_strategy", fake_run_strategy_set_flag)

    custom_strategy.analyze_custom_strategy()
    assert custom_strategy.get_dominance_guard_status() is True
