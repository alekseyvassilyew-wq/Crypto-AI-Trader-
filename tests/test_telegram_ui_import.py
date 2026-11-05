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




def test_scan_report_uses_compact_telegram_layout():
    from src.trader.telegram_ui import _format_scan_report

    report = _format_scan_report([
        {
            "symbol": "AZTECUSD", "signal": "HOLD", "score": 4,
            "turnover_eur": 95135, "price": 0.01471000,
            "ohlc_analyzed": True,
            "reasons": ["Bullish EMA trend", "RSI 60.2 supportive"],
        },
        {
            "symbol": "TONEUR", "signal": "HOLD", "score": 2,
            "turnover_eur": 33243, "price": 1.013,
            "spread_bps": 19.7, "ohlc_analyzed": True,
            "reasons": ["Trend not bullish", "RSI outside entry zone: 22.6"],
        },
        {
            "symbol": "TONUSD", "signal": "HOLD", "score": 2,
            "turnover_eur": 852319, "price": 1.135,
            "spread_bps": 8.8, "ohlc_analyzed": False,
            "reasons": ["Trend not bullish", "RSI outside entry zone: 22.7"],
        },
    ], 1348, 1326)

    assert report.startswith("🔎 KRAKEN · СКАНИРОВАНИЕ\n━━━━━━━━━━━━━━━━━━")
    assert "🌐 1,348 рынков  ·  1,326 тикеров" in report
    assert "🕯 EUR + OHLC: 2  ·  🟢 BUY: 0" in report
    assert "🟡 AZTECUSD  HOLD · 4/5" in report
    assert "Оборот €95.1k/24ч  ·  Цена 0.01471" in report
    assert "Bullish EMA trend · RSI 60.2 supportive" in report
    assert "🪙 TON · ОТДЕЛЬНАЯ ПРОВЕРКА" in report
    assert "🟡 TONEUR · HOLD  |  19.7 bps  |  €33.2k/24ч  |  OHLC ✓" in report
    assert "🟡 TONUSD · HOLD  |  8.8 bps  |  €852.3k/24ч  |  OHLC отложен" in report
    assert "анализ свечей ограничен API-бюджетом" in report
    assert len(report) < 3900


def test_scan_report_handles_missing_rows_and_values():
    from src.trader.telegram_ui import _format_scan_report

    report = _format_scan_report([], 0, 0)

    assert "0 рынков  ·  0 тикеров" in report
    assert "Нет пар, прошедших анализ свечей в этом цикле." in report
    assert "TONEUR и TONUSD не найдены" in report

def test_research_history_uses_readable_blocks_and_status_labels():
    from src.trader.telegram_ui import _format_research_history

    pages = _format_research_history([{
        "created_at": "2026-11-05T13:38:33.642994+00:00",
        "symbol": "PEPEEUR",
        "outcome": "NOT_PASS",
        "oos_return_pct": -15.30,
        "holdout_return_pct": -0.34,
        "profit_factor": "0.47690461696436276",
        "max_drawdown_pct": 18.93,
        "trades": 65,
        "reasons": ["Отрицательная доходность", "Profit Factor ниже порога"],
        "report": {
            "out_of_sample": {"trades": 65},
            "probability_diagnostics": {
                "walk_forward_oos": {
                    "min": 0.306, "mean": 0.529, "median": 0.524, "max": 0.726,
                    "thresholds": {
                        "0.50": {"trades": 65, "net_return_pct": -23.77},
                        "0.52": {"trades": 52, "net_return_pct": -19.50},
                    },
                },
            },
            "regime_metrics": {
                "BULL": {"net_return_pct": -14.34, "observations": 75},
            },
        },
    }])

    assert len(pages) == 1
    assert "🔴 НЕ ПРОШЛА ФИЛЬТР" in pages[0]
    assert "🪙 PEPEEUR" in pages[0]
    assert "05.11.2026 13:38 UTC" in pages[0]
    assert "Walk-forward OOS: -15.30%" in pages[0]
    assert "PF: 0.48" in pages[0]
    assert "Причины отказа:" in pages[0]
    assert "Пороговая диагностика" in pages[0]
    assert "BULL: -14.34%  •  75 наблюдений" in pages[0]
    assert "SHADOW ONLY" in pages[0]


def test_research_history_splits_long_history_into_telegram_pages():
    from src.trader.telegram_ui import _format_research_history

    rows = [{
        "created_at": "2026-11-05T13:38:33+00:00",
        "symbol": f"COIN{i}EUR",
        "outcome": "NOT_PASS",
        "oos_return_pct": -1,
        "holdout_return_pct": -2,
        "profit_factor": "0.5",
        "max_drawdown_pct": 3,
        "trades": 20,
        "reasons": ["Причина отказа " * 12],
        "report": {},
    } for i in range(10)]

    pages = _format_research_history(rows, max_chars=700)

    assert len(pages) > 1
    assert all(len(page) <= 900 for page in pages)
    assert "ПРОДОЛЖЕНИЕ" in pages[1]
    assert "COIN0EUR" in pages[0]
    assert "COIN9EUR" in pages[-1]
