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


def test_paper_forward_report_has_readable_sections_and_warnings():
    from src.trader.telegram_ui import _format_paper_report

    report = _format_paper_report({
        "samples": 324,
        "measurement_span_seconds": 23 * 3600 + 20 * 60,
        "start_equity_eur": 19.9410,
        "end_equity_eur": 18.8259,
        "change_eur": -1.1152,
        "change_pct": -5.59,
        "max_observed_drawdown_pct": 5.71,
        "avg_open_positions": 1.98,
        "buy_signals": 306,
        "avg_analyzed_markets": 16.6,
        "closed_trades": 0,
        "closed_trades_without_eur_valuation": 2,
        "winning_trades": 0,
        "losing_trades": 0,
        "win_rate_pct": 0,
        "profit_factor": 0,
        "realized_pnl_eur": 0,
        "total_fees_eur": 0.0226,
        "fills_without_eur_fee_valuation": 2,
        "by_symbol": [],
        "first_ts": "2026-11-04 15:13:25",
        "last_ts": "2026-11-05 14:33:34",
    }, 24)

    assert report.startswith("📈 <b>PAPER FORWARD-TEST</b>")
    assert "🔴 <b>РЕЗУЛЬТАТ: СНИЖЕНИЕ</b>" in report
    assert "АКТИВНОСТЬ ПОРТФЕЛЯ" in report
    assert "ЗАКРЫТЫЕ СДЕЛКИ" in report
    assert "−" not in report  # signed numeric values use a consistent ASCII sign
    assert "+0.0000 EUR" in report
    assert "НЕТ ЗАКРЫТЫХ СДЕЛОК" in report
    assert "ЕСТЬ ОПЕРАЦИИ БЕЗ EUR-ОЦЕНКИ" in report
    assert "не гарантирует непрерывную работу" in report
