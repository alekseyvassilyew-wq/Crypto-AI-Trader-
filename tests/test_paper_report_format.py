from src.trader.telegram_ui import _format_paper_forward_report


def sample_metrics():
    return {
        "samples": 324,
        "start_equity_eur": 19.9410,
        "end_equity_eur": 18.8259,
        "change_eur": -1.1152,
        "change_pct": -5.59,
        "max_observed_drawdown_pct": 5.71,
        "avg_open_positions": 1.98,
        "buy_signals": 306,
        "avg_analyzed_markets": 16.6,
        "closed_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "win_rate_pct": 0,
        "profit_factor": 0,
        "realized_pnl_eur": 0,
        "total_fees_eur": 0.0226,
        "closed_trades_without_eur_valuation": 2,
        "fills_without_eur_fee_valuation": 2,
        "first_ts": "2026-11-04T15:13:25+00:00",
        "last_ts": "2026-11-05T14:33:34+00:00",
    }


def test_paper_forward_report_has_clear_sections_and_preserves_metrics():
    report = _format_paper_forward_report(
        sample_metrics(), 24, "пока нет закрытых сделок", "23 ч 20 мин", 97.2
    )

    assert report.startswith("📈 PAPER FORWARD-TEST\n━━━━━━━━━━━━━━━━━━")
    assert "💼 КАПИТАЛ" in report
    assert "📉 РИСК И ПОЗИЦИИ" in report
    assert "🔎 АКТИВНОСТЬ СКАНЕРА" in report
    assert "📊 ЗАКРЫТЫЕ СДЕЛКИ" in report
    assert "💸 КОМИССИИ И ОЦЕНКА" in report
    assert "19.9410 EUR" in report
    assert "18.8259 EUR" in report
    assert "-1.1152 EUR  (-5.59%)" in report
    assert "5.71%" in report
    assert "Сигналов BUY: 306" in report
    assert "Без EUR-оценки" not in report  # counters are explicitly labelled below
    assert "Сделок без EUR-оценки: 2" in report
    assert "Исполнений без EUR-оценки комиссии: 2" in report
    assert "04.11.2026 15:13 — 05.11.2026 14:33 UTC" in report
    assert "не доказательство будущей доходности" in report


def test_paper_forward_report_formats_infinite_profit_factor_and_missing_values():
    metrics = sample_metrics()
    metrics["profit_factor"] = "INF"
    metrics["avg_open_positions"] = None
    report = _format_paper_forward_report(metrics, 24, "", "23 ч 20 мин", 97.2)

    assert "Profit factor: ∞" in report
    assert "Среднее число открытых позиций: н/д" in report
    assert "Пока нет закрытых сделок" in report
