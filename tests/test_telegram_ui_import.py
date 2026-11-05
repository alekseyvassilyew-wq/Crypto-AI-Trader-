def test_telegram_ui_module_imports_without_starting_the_bot():
    from src.trader import telegram_ui

    assert callable(telegram_ui.build_app)

def test_automatic_research_report_formats_metrics_and_rejections():
    from src.trader.telegram_ui import _format_automatic_research_report

    report = _format_automatic_research_report([{
        "symbol": "NEAREUR",
        "pass": False,
        "samples": 659,
        "oos_return": 7.28,
        "holdout_return": -9.02,
        "oos_metrics": {
            "trades": 4,
            "profit_factor": 1.08,
            "max_drawdown_pct": 3.5,
        },
        "holdout_metrics": {
            "trades": 2,
            "max_drawdown_pct": 9.02,
        },
        "benchmarks": {
            "walk_forward_oos": {
                "always_long": {"net_return_pct": -51.30},
                "positive_24h_momentum": {"net_return_pct": 2.0},
            },
            "independent_holdout": {
                "always_long": {"net_return_pct": -23.96},
                "positive_24h_momentum": {"net_return_pct": 1.0},
            },
        },
        "auc_oos": 0.552132,
        "auc_holdout": 0.51,
        "reasons": ["Недостаточно сделок", "Не превзошла benchmark <holdout>"],
    }])

    assert "🔴 <b>NEAREUR — NOT PASSED</b>" in report
    assert "Выборок: <b>659</b>" in report
    assert "Сделок: <b>4</b>" in report
    assert "OOS <b>0.552</b>" in report
    assert "SHADOW ONLY" in report
    assert "benchmark &lt;holdout&gt;" in report
    assert "Недостаточно сделок" in report


def test_automatic_research_report_marks_passed_models():
    from src.trader.telegram_ui import _format_automatic_research_report

    report = _format_automatic_research_report([{
        "symbol": "XBTEUR",
        "pass": True,
        "samples": 700,
        "oos_return": 3.0,
        "holdout_return": 2.0,
        "oos_metrics": {"trades": 20, "profit_factor": "INF"},
        "holdout_metrics": {"trades": 5},
        "benchmarks": {},
        "auc_oos": None,
        "auc_holdout": None,
        "reasons": [],
    }])

    assert "🟢 <b>XBTEUR — PASSED</b>" in report
    assert "PF: <b>∞</b>" in report
    assert "Все исследовательские критерии пройдены." in report

