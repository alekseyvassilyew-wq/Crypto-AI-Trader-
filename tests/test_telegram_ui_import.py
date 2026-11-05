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




def test_scan_report_matches_monitor_status_plain_text_layout():
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

    assert report.startswith("🔎 СКАНИРОВАНИЕ РЫНКА KRAKEN\n━━━━━━━━━━━━━━━━━━\n🌐 СТАТИСТИКА РЫНКА\nВсего рынков Kraken: 1,348")
    assert "Тикеров получено: 1,325" in report
    assert "Сигналов BUY: 0" in report
    assert "KNTQUSD: HOLD (3/5), оборот 141,407 EUR/24ч, цена 0.46052" in report
    assert "  Причина: Trend not bullish; RSI 52.8 supportive" in report
    assert "Все рынки получают мониторинг котировок; анализ свечей ограничен бюджетом запросов." in report
    assert "TONEUR: HOLD | спред 19.6 bps | оборот 30,894 EUR/24ч | OHLC проверен" in report
    assert "TONUSD: HOLD | спред 8.7 bps | оборот 847,000 EUR/24ч | OHLC отложен" in report
