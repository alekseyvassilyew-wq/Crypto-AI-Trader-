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



def test_scan_report_formats_market_stats_and_ton_section():
    from src.trader.telegram_ui import _format_scan_report

    report = _format_scan_report([
        {
            "symbol": "KNTQUSD", "signal": "HOLD", "score": 3,
            "turnover_eur": 141407, "price": 0.46052,
            "ohlc_analyzed": True,
            "reasons": ["Trend not bullish", "RSI 52.8 supportive"],
        },
        {
            "symbol": "TONEUR", "signal": "HOLD", "score": 2,
            "turnover_eur": 30894, "price": 1.14,
            "spread_bps": 19.6, "ohlc_analyzed": True,
            "reasons": ["Trend not bullish"],
        },
        {
            "symbol": "TONUSD", "signal": "HOLD", "score": 2,
            "turnover_eur": 847000, "price": 1.148,
            "spread_bps": 8.7, "ohlc_analyzed": False,
            "reasons": ["OHLC analysis deferred"],
        },
    ], 1348, 1325)

    assert "🔎 <b>СКАНИРОВАНИЕ РЫНКА KRAKEN</b>" in report
    assert "BUY-СИГНАЛОВ НЕТ" in report
    assert "Рынков Kraken: <b>1,348</b>" in report
    assert "Получено тикеров: <b>1,325</b>" in report
    assert "KNTQUSD" in report and "141,407 EUR/24ч" in report
    assert "TONEUR" in report and "19.6 bps" in report
    assert "TONUSD" in report and "OHLC отложен" in report
    assert "RSI 52.8 supportive" in report


def test_scan_report_escapes_dynamic_html_and_marks_buy_signal():
    from src.trader.telegram_ui import _format_scan_report

    report = _format_scan_report([{
        "symbol": "<XBTEUR>",
        "signal": "BUY",
        "score": 5,
        "turnover_eur": 1000,
        "price": 60000,
        "ohlc_analyzed": True,
        "reasons": ["Strong <trend> & momentum"],
    }], 10, 9)

    assert "🟢 <b>НАЙДЕНЫ BUY-СИГНАЛЫ</b>" in report
    assert "&lt;XBTEUR&gt;" in report
    assert "Strong &lt;trend&gt; &amp; momentum" in report
    assert "<XBTEUR>" not in report
