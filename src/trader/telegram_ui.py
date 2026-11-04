import logging
import html
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

log = logging.getLogger(__name__)

def build_app(s, engine, exchange):
    def fresh_position_marks():
        """Return recent executable bid-side marks for every open PAPER position."""
        from datetime import datetime, timezone
        import math

        needs_market_marks = bool(engine.paper.positions) or any(
            str(currency).upper() != "EUR" and float(amount) > 1e-12
            for currency, amount in engine.paper.cash_by_currency.items()
        )
        if not needs_market_marks:
            return True, {}
        scanned_at = engine.last_scan_at
        if scanned_at is None:
            return False, {}
        age = (datetime.now(timezone.utc) - scanned_at).total_seconds()
        if age < 0 or age > max(120, 2 * int(engine.scan_interval_seconds)):
            return False, {}
        marks = {}
        for symbol in engine.paper.positions:
            ticker = engine.last_ticker_data.get(str(symbol).upper())
            try:
                bid = float(ticker.get("bid1Price") or 0) if ticker else 0.0
                ask = float(ticker.get("ask1Price") or 0) if ticker else 0.0
                last = float(engine.last_prices.get(str(symbol).upper()) or 0)
            except (TypeError, ValueError, OverflowError):
                return False, {}
            if (not all(math.isfinite(value) and value > 0 for value in (bid, ask, last))
                    or ask < bid):
                return False, {}
            marks[str(symbol).upper()] = bid
        return True, marks

    def safe_error_message(exc):
        message = str(exc) or "Без дополнительного описания"
        if s.bot_token:
            message = message.replace(s.bot_token, "[REDACTED]")
        return f"{type(exc).__name__}: {message[:500]}"

    def format_error(exc, action, user=None, source="Crypto AI Trader"):
        import traceback
        if user:
            username = f"@{user.username}" if user.username else "без username"
            who = f"{user.id} ({username})"
        else:
            who = "— (—)"
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if s.bot_token:
            trace = trace.replace(s.bot_token, "[REDACTED]")
        description = safe_error_message(exc)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return (
            "🚨 <b>ОШИБКА БОТА</b>\n"
            f"🕒 Время: <code>{timestamp}</code>\n"
            f"🧩 Источник: <b>{html.escape(source)}</b>\n"
            f"👤 Кто: {html.escape(who)}\n"
            f"💬 Действие: {html.escape(str(action))}\n"
            f"⚠️ Тип: <code>{html.escape(type(exc).__name__)}</code>\n"
            f"📝 Описание: {html.escape(description)}\n"
            "📋 <b>Трейс:</b>\n"
            f"<pre>{html.escape(trace[-2600:])}</pre>"
        )[:3900]

    async def send_topic(context, topic_id, text, parse_mode=None):
        if not s.telegram_group_chat_id:
            return
        kwargs = {"chat_id": s.telegram_group_chat_id, "text": text[:3900]}
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_message(**kwargs)

    async def authorized(update):
        return bool(update.effective_user and update.effective_user.id == s.admin_id)

    async def deny(update):
        if update.effective_message: await update.effective_message.reply_text("Доступ запрещён.")

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await authorized(update): return await deny(update)
        keys = [[InlineKeyboardButton("📊 Сканировать", callback_data="scan"), InlineKeyboardButton("💼 Портфель", callback_data="portfolio")],
                [InlineKeyboardButton("▶️ Запуск PAPER", callback_data="run"), InlineKeyboardButton("⏸ Пауза", callback_data="pause")],
                [InlineKeyboardButton("⛔ STOP", callback_data="stop")]]
        await update.effective_message.reply_text("Crypto AI Trader\nТолько PAPER. Реальные ордера технически не поддерживаются.", reply_markup=InlineKeyboardMarkup(keys))

    async def status(update, context):
        if not await authorized(update): return await deny(update)
        def read_status():
            state = "PAUSED" if engine.paused else "RUNNING"
            balances = ", ".join(f"{currency}: {amount:.6f}" for currency, amount in sorted(engine.paper.cash_by_currency.items()))
            fresh_rates = engine._fresh_fx_rates(engine.last_prices)
            required = {
                currency for currency, amount in engine.paper.cash_by_currency.items()
                if float(amount) > 1e-12
            }
            required.update(
                str(position.get("quote_currency", "EUR")).upper()
                for position in engine.paper.positions.values()
            )
            missing = sorted(currency for currency in required if currency not in fresh_rates)
            quotes_fresh, bid_marks = fresh_position_marks()
            valuation_prices = dict(engine.last_prices)
            valuation_prices.update(bid_marks)
            if missing:
                daily_text = "недоступен — нет свежего FX: " + ", ".join(missing)
            elif not quotes_fresh:
                daily_text = "недоступен — нет свежих рыночных котировок для оценки портфеля"
            else:
                daily_text = f"{engine.daily_pnl(valuation_prices):.2f} EUR"
            return (
                f"Режим: PAPER\nСостояние: {state}\nВиртуальные средства: {balances}\n"
                f"Позиции: {len(engine.paper.positions)}/{s.max_positions}\nP/L сегодня: {daily_text}\n"
                f"Риск: SL {s.stop_loss_pct:.2f}% | TP {s.take_profit_pct:.2f}% | trailing {getattr(s, 'trailing_stop_pct', 1.5):.2f}%\n"
                f"Рынок Kraken: {len(engine.market_pairs)} пар; котировок получено: {engine.market_ticker_count}\n"
                f"Мониторинг всех котировок; анализ свечей: до {s.max_candle_pairs} пар за цикл.\n"
                "* P/L показывается только при наличии свежих EUR-курсов для всех валют кошелька."
            )
        message = await context.application.bot_data["run_blocking"].run(read_status)
        await update.effective_message.reply_text(message)

    async def scan(update, context):
        if not await authorized(update): return await deny(update)
        await update.effective_message.reply_text("Сканирование рынка…")
        try:
            def run_scan():
                rows = engine.scan()
                return rows, len(engine.market_pairs), engine.market_ticker_count
            rows, pair_count, ticker_count = await context.application.bot_data["run_blocking"].run(run_scan)
            buy_rows = [x for x in rows if x.get("signal") == "BUY"]
            analyzed = [x for x in rows if x.get("ohlc_analyzed") is True]
            ranked = sorted(
                analyzed,
                key=lambda x: (x.get("score", 0), x.get("turnover_eur", 0)),
                reverse=True,
            )[:12]
            lines = [
                f"Всего рынков Kraken: {pair_count}",
                f"Тикеров получено: {ticker_count}",
                f"Пар с доступным EUR-маршрутом, прошедших анализ свечей: {len(analyzed)}",
                f"Сигналов BUY: {len(buy_rows)}",
                "",
            ]
            lines.extend(
                f"{x['symbol']}: {x['signal']} ({x.get('score', 0)}/5), "
                f"оборот {x.get('turnover_eur', 0):,.0f} EUR/24ч, цена {x.get('price', 0):,.8g}\n"
                f"  Причина: {'; '.join(x.get('reasons', [])[:2]) or 'Нет дополнительных причин'}"
                for x in ranked
            )
            lines.append("Все рынки получают мониторинг котировок; анализ свечей ограничен бюджетом запросов.")
            await update.effective_message.reply_text("\n".join(lines)[:3900])
        except Exception as exc:
            # Keep the full traceback in Render logs and expose the actionable
            # exception type/message in the private owner Telegram response.
            log.error("Market scan failed: %s", type(exc).__name__)
            await update.effective_message.reply_text(
                format_error(exc, "/scan", update.effective_user), parse_mode="HTML"
            )

    async def backtest(update, context):
        if not await authorized(update): return await deny(update)
        if not context.args or len(context.args) != 1:
            return await update.effective_message.reply_text(
                "Использование: /backtest XBTEUR или /backtest portfolio\n"
                "Проверяет закрытые часовые свечи Kraken. Ничего не покупает и не меняет PAPER-кошелёк."
            )
        symbol = context.args[0].strip().upper().replace("/", "")
        if symbol == "PORTFOLIO":
            await update.effective_message.reply_text("Мульти-рынковый исторический PAPER-тест: выбираю до 5 наиболее ликвидных рынков и загружаю закрытые часовые свечи…")
            try:
                def run_portfolio():
                    from .portfolio_backtest import run_portfolio_backtest
                    pairs = exchange.discover_pairs()
                    tickers = exchange.tickers()
                    engine.market_pairs = pairs
                    by_symbol = {x["symbol"]: x for x in tickers}
                    engine.last_ticker_data = by_symbol
                    prices = {x["symbol"]: float(x.get("lastPrice") or 0) for x in tickers if float(x.get("lastPrice") or 0) > 0}
                    fx_rates = engine._fresh_fx_rates(prices)
                    fx_buy_rates = engine._fresh_fx_buy_rates(prices)
                    eligible = []
                    for pair in pairs:
                        ticker = by_symbol.get(pair["symbol"])
                        quote = str(pair.get("quote") or "").upper()
                        rate = fx_rates.get(quote, 0.0)
                        if ticker and rate > 0:
                            turnover_eur = float(ticker.get("turnover24h") or 0) * rate
                            if turnover_eur >= s.min_turnover:
                                eligible.append((turnover_eur, pair["symbol"], quote))
                    eligible.sort(reverse=True)
                    selected = eligible[:5]
                    if len(selected) < 2:
                        raise RuntimeError("Недостаточно ликвидных рынков с доступным EUR-маршрутом для портфельного теста.")
                    candles_by_symbol = {}
                    quote_currencies = {}
                    failures = []
                    for _, market_symbol, quote in selected:
                        try:
                            candles_by_symbol[market_symbol] = exchange.candles(market_symbol, interval="60", limit=720)
                            quote_currencies[market_symbol] = quote
                        except Exception as exc:
                            failures.append(f"{market_symbol}: {type(exc).__name__}")
                    if len(candles_by_symbol) < 2:
                        raise RuntimeError("Не удалось загрузить минимум два рынка: " + ", ".join(failures))
                    # Align markets on their shared closed-candle timestamps; Kraken
                    # may omit an individual candle for a thin/inactive pair.
                    common = set(row[0] for rows in candles_by_symbol.values() for row in rows)
                    for rows in candles_by_symbol.values():
                        common.intersection_update(row[0] for row in rows)
                    common_timestamps = sorted(common)
                    if len(common_timestamps) < 62:
                        raise RuntimeError("После синхронизации у рынков осталось менее 62 общих часовых свечей.")
                    candles_by_symbol = {
                        market: [row for row in rows if row[0] in common]
                        for market, rows in candles_by_symbol.items()
                    }
                    # Fetch direct EUR FX markets for the selected quote currencies.
                    # If Kraken has no direct EUR market, the backtester explicitly
                    # falls back to the current snapshot rate for that currency.
                    dated_fx_by_timestamp = {}
                    dated_fx_buy_by_timestamp = {}
                    fx_fallback = []
                    for currency in sorted(set(quote_currencies.values()) - {"EUR"}):
                        fx_pair = next(
                            (pair for pair in pairs
                             if (pair["base"] == currency and pair["quote"] == "EUR")
                             or (pair["base"] == "EUR" and pair["quote"] == currency)),
                            None,
                        )
                        if not fx_pair:
                            fx_fallback.append(currency)
                            continue
                        try:
                            fx_rows = exchange.candles(fx_pair["symbol"], interval="60", limit=720)
                            direct = fx_pair["base"] == currency
                            matched_fx_timestamps = set()
                            for row in fx_rows:
                                if row[0] not in common:
                                    continue
                                dated_fx_by_timestamp.setdefault(row[0], {"EUR": 1.0})[currency] = (
                                    row[4] if direct else 1.0 / row[4]
                                )
                                dated_fx_buy_by_timestamp.setdefault(row[0], {"EUR": 1.0})[currency] = (
                                    1.0 / row[4] if direct else row[4]
                                )
                                matched_fx_timestamps.add(row[0])
                            if len(matched_fx_timestamps) < len(common):
                                fx_fallback.append(currency)
                        except Exception:
                            log.error("Historical FX candles unavailable for %s", currency)
                            fx_fallback.append(currency)
                    result = run_portfolio_backtest(
                        candles_by_symbol,
                        quote_currencies=quote_currencies,
                        fx_rates=fx_rates,
                        fx_rates_by_timestamp=dated_fx_by_timestamp,
                        fx_buy_rates=fx_buy_rates,
                        fx_buy_rates_by_timestamp=dated_fx_buy_by_timestamp,
                        initial_cash_eur=s.paper_start,
                        max_trade_eur=s.max_trade,
                        max_positions=s.max_positions,
                        risk_per_trade_pct=s.risk_per_trade_pct,
                        fee_rate=s.fee_rate,
                        slippage_bps=5.0,
                        stop_loss_pct=s.stop_loss_pct,
                        take_profit_pct=s.take_profit_pct,
                        trailing_stop_pct=s.trailing_stop_pct,
                        max_daily_loss_eur=s.max_daily_loss,
                    )
                    return result, failures, fx_fallback
                result, failures, fx_fallback = await context.application.bot_data["run_blocking"].run(run_portfolio)
                factor = result["profit_factor"]
                factor_text = "∞" if factor == float("inf") else f"{factor:.2f}"
                lines = [
                    "📊 ПОРТФЕЛЬНЫЙ ИСТОРИЧЕСКИЙ PAPER-ТЕСТ",
                    f"Рынки: {', '.join(result['markets'])}",
                    f"Начальный капитал: {result['initial_equity_eur']:.2f} EUR",
                    f"Итоговый капитал: {result['final_equity_eur']:.2f} EUR",
                    f"Чистый P/L: {result['net_pnl_eur']:+.4f} EUR ({result['return_pct']:+.2f}%)",
                    f"Buy & hold (равновесный): {result['buy_hold_return_pct']:+.2f}%",
                    f"Сделок: {result['trades']} | прибыльных: {result['wins']} | убыточных: {result['losses']}",
                    f"Win rate: {result['win_rate_pct']:.1f}% | Profit factor: {factor_text}",
                    f"Макс. просадка: {result['max_drawdown_pct']:.2f}%",
                    f"Комиссии: {result['fees_paid_eur']:.4f} EUR",
                    f"Комиссия: {s.fee_rate * 100:.3f}% на сторону; проскальзывание: 5 bps на сторону.",
                    f"Исторические FX-свечи: {result['assumptions']['dated_fx_rate_timestamps']} временных точек. Текущий FX использован как запасной курс для: {', '.join(fx_fallback) if fx_fallback else 'нет валют'}.",
                    "Результат — симуляция, не прогноз прибыли и не гарантия будущей доходности.",
                ]
                if failures:
                    lines.append("Рынки, пропущенные при загрузке: " + ", ".join(failures))
                await update.effective_message.reply_text("\n".join(lines)[:3900])
            except Exception as exc:
                log.error("Portfolio historical backtest failed: %s", type(exc).__name__)
                await update.effective_message.reply_text(format_error(exc, "/backtest portfolio", update.effective_user))
            return
        await update.effective_message.reply_text(f"Исторический тест {symbol}…")
        try:
            from .backtest import run_backtest
            def run_single_backtest():
                pair = next(
                    (item for item in exchange.discover_pairs() if item["symbol"] == symbol),
                    None,
                )
                if pair is None:
                    raise ValueError(f"Kraken Spot market {symbol} was not found.")
                if str(pair.get("quote") or "").upper() != "EUR":
                    raise ValueError(
                        "Single-market backtests use EUR-quoted pairs only. "
                        "Use /backtest portfolio for USD/USDT and other quote currencies."
                    )
                return run_backtest(
                    exchange.candles(symbol, interval="60", limit=720),
                    initial_cash=s.paper_start,
                    max_trade=s.max_trade,
                    fee_rate=s.fee_rate,
                    slippage_bps=5.0,
                    stop_loss_pct=s.stop_loss_pct,
                    take_profit_pct=s.take_profit_pct,
                    trailing_stop_pct=s.trailing_stop_pct,
                    risk_per_trade_pct=s.risk_per_trade_pct,
                )
            result = await context.application.bot_data["run_blocking"].run(run_single_backtest)
            factor = result["profit_factor"]
            factor_text = "∞" if factor == float("inf") else f"{factor:.2f}"
            await update.effective_message.reply_text(
                f"📉 ИСТОРИЧЕСКИЙ PAPER-ТЕСТ: {symbol}\n"
                f"Свечей: {result['candles']} (часовые, закрытые)\n"
                f"Начальный капитал: {result['initial_cash']:.2f}\n"
                f"Итоговый капитал: {result['final_equity']:.2f}\n"
                f"Чистый P/L: {result['net_pnl']:+.4f} ({result['return_pct']:+.2f}%)\n"
                f"Сделок: {result['trades']} | прибыльных: {result['wins']} | убыточных: {result['losses']}\n"
                f"Win rate: {result['win_rate_pct']:.1f}%\n"
                f"Profit factor: {factor_text}\n"
                f"Макс. просадка: {result['max_drawdown_pct']:.2f}%\n"
                f"Buy & hold: {result['buy_hold_return_pct']:+.2f}%\n"
                f"Комиссии всего: {result['fees_paid']:.6f}\n"
                f"Комиссии вход/выход: {result['entry_fees_paid']:.6f} / {result['exit_fees_paid']:.6f}\n"
                f"Оборот: {result['gross_turnover']:.4f}; ставка: {result['fee_rate'] * 100:.3f}% на сторону\n"
                "Издержки: комиссия на вход/выход + проскальзывание 5 bps на сторону. "
                "Исполнение моделируется по открытию следующей свечи. "
                "Это тест одного рынка, не прогноз и не гарантия будущей прибыли."
            )
        except Exception as exc:
            log.error("Historical backtest failed for %s: %s", symbol, type(exc).__name__)
            await update.effective_message.reply_text(
                format_error(exc, f"/backtest {symbol}", update.effective_user)
            )

    async def account(update, context):
        if not await authorized(update): return await deny(update)
        def read_account():
            balances = [f"{currency}: {amount:.8f}" for currency, amount in sorted(engine.paper.cash_by_currency.items())]
            return "Виртуальные остатки PAPER:\n" + ("\n".join(balances) or "Нулевые остатки.")
        message = await context.application.bot_data["run_blocking"].run(read_account)
        await update.effective_message.reply_text(message)

    async def realbalance(update, context):
        if not await authorized(update): return await deny(update)
        if not s.kraken_api_key or not s.kraken_api_secret:
            return await update.effective_message.reply_text(
                "Реальный баланс Kraken недоступен: не заданы API-ключи. "
                "Нужны только права Query Funds. Это чтение не меняет PAPER-кошелёк."
            )
        await update.effective_message.reply_text("Запрашиваю реальные остатки Kraken (только чтение)…")
        try:
            balances = await context.application.bot_data["run_blocking"].run(exchange.balance)
            lines = []
            for asset, raw_amount in sorted(balances.items()):
                try:
                    amount = float(raw_amount)
                except (TypeError, ValueError, OverflowError):
                    continue
                if amount > 0:
                    lines.append(f"{asset}: {amount:.10f}")
            await update.effective_message.reply_text(
                "🏦 РЕАЛЬНЫЙ БАЛАНС KRAKEN\n"
                + ("\n".join(lines) if lines else "Положительных остатков не найдено.")
                + "\n\nТолько чтение. Реальные средства не импортируются в виртуальный PAPER-кошелёк."
            )
        except Exception as exc:
            log.error("Read-only Kraken balance request failed: %s", type(exc).__name__)
            await update.effective_message.reply_text(format_error(exc, "/realbalance", update.effective_user))

    async def learn(update, context):
        if not await authorized(update): return await deny(update)
        if len(context.args) != 1:
            return await update.effective_message.reply_text("Использование: /learn XBTEUR — исследование модели на закрытых часовых свечах.")
        symbol = context.args[0].strip().upper().replace("/", "")
        await update.effective_message.reply_text(
            f"🧠 Исследую {symbol}: 3 хронологических walk-forward проверки, независимый holdout и оценка bull/bear/sideways. "
            "Это не запускает и не меняет торговлю."
        )
        try:
            def run_research():
                from .research_history import perform_research
                return perform_research(engine.db, exchange, s, symbol, source="manual")
            candidate = await context.application.bot_data["run_blocking"].run(run_research)
            metrics = candidate["out_of_sample"]
            pf = metrics["profit_factor"]
            pf_text = "∞" if pf == "INF" else f"{pf:.2f}"
            benchmarks = candidate.get("benchmark_metrics", {})
            oos_benchmarks = benchmarks.get("walk_forward_oos", {})
            holdout_benchmarks = benchmarks.get("independent_holdout", {})
            ranking = candidate.get("score_ranking", {})
            def auc_text(value):
                return "н/д" if value is None else f"{float(value):.3f}"
            gate = "ПРОШЛА исследовательский фильтр" if candidate["research_pass"] else "НЕ прошла исследовательский фильтр"
            await update.effective_message.reply_text(
                f"🧠 МОДЕЛЬ {symbol} — {gate}\n"
                f"Статус: SHADOW ONLY (не торгует)\n"
                f"Обучающих/тестовых выборок: {candidate['samples']}\n"
                f"Walk-forward фолдов: {len(candidate['walk_forward'])}/3\n"
                f"Вневыборочных сделок: {metrics['trades']}\n"
                f"Доходность после издержек: {metrics['net_return_pct']:+.2f}%\n"
                f"Win rate: {metrics['win_rate_pct']:.1f}% | Profit factor: {pf_text}\n"
                f"Макс. просадка OOS: {metrics['max_drawdown_pct']:.2f}%\n"
                f"Benchmark OOS: BUY&HOLD {oos_benchmarks.get('always_long', {}).get('net_return_pct', 0):+.2f}% | "
                f"Momentum {oos_benchmarks.get('positive_24h_momentum', {}).get('net_return_pct', 0):+.2f}%\n"
                f"Benchmark holdout: BUY&HOLD {holdout_benchmarks.get('always_long', {}).get('net_return_pct', 0):+.2f}% | "
                f"Momentum {holdout_benchmarks.get('positive_24h_momentum', {}).get('net_return_pct', 0):+.2f}%\n"
                f"ROC AUC (ranking only): OOS {auc_text(ranking.get('walk_forward_oos_roc_auc'))} | "
                f"holdout {auc_text(ranking.get('independent_holdout_roc_auc'))}\n"
                f"Независимый holdout: {candidate['independent_holdout']['net_return_pct']:+.2f}% | "
                f"{candidate['independent_holdout']['trades']} сделок | "
                f"DD {candidate['independent_holdout']['max_drawdown_pct']:.2f}%\n"
                "Режимы рынка (OOS): " + " | ".join(
                    f"{regime}: {data['observations']} наблюдений, {data['net_return_pct']:+.2f}%"
                    for regime, data in candidate.get("regime_metrics", {}).items()
                ) + "\n"
                + ("Причины отказа:\n" + "\n".join("• " + reason for reason in candidate.get("rejection_reasons", [])[:6])
                   if candidate.get("rejection_reasons") else "Все исследовательские критерии пройдены.") + "\n"
                + "Диагностика вероятностей (не подбор торгового порога):\n"
                + "\n".join(
                    f"Порог {level}: {data['trades']} сделок, P/L {data['net_return_pct']:+.2f}%"
                    for level, data in candidate.get("probability_diagnostics", {}).get("walk_forward_oos", {}).get("thresholds", {}).items()
                ) + "\n"
                + (f"Вероятность: min {candidate['probability_diagnostics']['walk_forward_oos']['min']:.3f}, "
                   f"средняя {candidate['probability_diagnostics']['walk_forward_oos']['mean']:.3f}, "
                   f"медиана {candidate['probability_diagnostics']['walk_forward_oos']['median']:.3f}, "
                   f"max {candidate['probability_diagnostics']['walk_forward_oos']['max']:.3f}\n"
                   if candidate.get("probability_diagnostics", {}).get("walk_forward_oos", {}).get("min") is not None
                   else "Нет вневыборочных прогнозов.\n")
                + f"Исторических свечей накоплено: {candidate.get('history_candles', candidate['samples'])}\n"
                "Модель остаётся SHADOW ONLY: автоматического внедрения в торговлю нет."
            )
        except Exception as exc:
            log.error("ML research failed for %s: %s", symbol, type(exc).__name__)
            await update.effective_message.reply_text(format_error(exc, f"/learn {symbol}", update.effective_user))

    async def learning(update, context):
        if not await authorized(update): return await deny(update)
        def read_models():
            from .db import get_state, get_shadow_metrics
            rows = engine.db.execute(
                "SELECT key FROM app_state WHERE key LIKE 'ml_candidate_%' ORDER BY updated_at DESC"
            ).fetchall()
            lines = ["🧠 ИССЛЕДОВАТЕЛЬСКИЕ МОДЕЛИ (не торгуют):"]
            for row in rows[:15]:
                item = get_state(engine.db, row["key"], {})
                metric = item.get("out_of_sample", {})
                symbol = item.get("symbol", row["key"].replace("ml_candidate_", ""))
                shadow = get_shadow_metrics(engine.db, symbol, item.get("model_version", ""))
                lines.append(
                    f"{symbol}: {'PASS' if item.get('research_pass') else 'NOT PASS'} | "
                    f"{metric.get('net_return_pct', 0):+.2f}% OOS | "
                    f"PF {metric.get('profit_factor', 0)} | "
                    f"SHADOW {shadow['shadow_trades']} trades / {shadow['resolved_predictions']} resolved | "
                    f"{shadow['net_return_pct']:+.2f}% | SHADOW ONLY"
                )
            return lines
        lines = await context.application.bot_data["run_blocking"].run(read_models)
        if len(lines) == 1:
            return await update.effective_message.reply_text("Исследовательских моделей пока нет. Запусти /learn XBTEUR.")
        await update.effective_message.reply_text("\n".join(lines)[:3900])

    async def research_history_cmd(update, context):
        if not await authorized(update): return await deny(update)
        symbol = context.args[0].strip().upper().replace("/", "") if context.args else None
        try:
            def read_history():
                from .research_history import list_research_runs
                return list_research_runs(engine.db, limit=10, symbol=symbol)
            rows = await context.application.bot_data["run_blocking"].run(read_history)
            if not rows:
                return await update.effective_message.reply_text(
                    "История исследований пока пуста. Запусти /learn XBTEUR или дождись автоматического исследования."
                )
            lines = ["🧠 ИСТОРИЯ ИССЛЕДОВАНИЙ — SHADOW ONLY"]
            for item in rows:
                report = item.get("report") or {}
                oos = report.get("out_of_sample") or {}
                holdout = report.get("independent_holdout") or {}
                reasons = item.get("reasons") or []
                lines.append(
                    f"{item['created_at']} | {item['symbol']} | {item['outcome']} | "
                    f"OOS {float(item.get('oos_return_pct') or 0):+.2f}% | "
                    f"holdout {float(item.get('holdout_return_pct') or 0):+.2f}% | "
                    f"PF {item.get('profit_factor', 'N/A')} | DD {float(item.get('max_drawdown_pct') or 0):.2f}%"
                )
                if reasons:
                    lines.append("Причина: " + "; ".join(str(reason) for reason in reasons[:3]))
                probability = report.get("probability_diagnostics", {}).get("walk_forward_oos", {})
                if probability:
                    lines.append("Вероятности: " + (
                        f"min {probability['min']:.3f}, mean {probability['mean']:.3f}, "
                        f"median {probability['median']:.3f}, max {probability['max']:.3f}"
                        if probability.get("min") is not None else "нет прогнозов"
                    ))
                    thresholds = probability.get("thresholds") or {}
                    if thresholds:
                        lines.append("Пороги (диагностика): " + " | ".join(
                            f"{level}: {metrics.get('trades', 0)} trades / {metrics.get('net_return_pct', 0):+.2f}%"
                            for level, metrics in thresholds.items()
                        ))
                regimes = report.get("regime_metrics") or {}
                if regimes:
                    lines.append("Режимы: " + " | ".join(
                        f"{name} {data.get('net_return_pct', 0):+.2f}%/{data.get('observations', 0)} obs"
                        for name, data in regimes.items()
                    ))
            await update.effective_message.reply_text("\n".join(lines)[:3900])
        except Exception as exc:
            await update.effective_message.reply_text(f"Ошибка истории исследований: {safe_error_message(exc)}")

    async def paperreport(update, context):
        if not await authorized(update): return await deny(update)
        from .db import get_paper_metrics
        try:
            hours = int(context.args[0]) if context.args else 24
            metrics = await context.application.bot_data["run_blocking"].run(
                lambda: get_paper_metrics(engine.db, hours)
            )
            if not metrics["samples"]:
                return await update.effective_message.reply_text(
                    f"За последние {hours} ч. нет сохранённых PAPER-измерений. "
                    "Запусти /run и оставь PAPER работать для сбора статистики."
                )
            await update.effective_message.reply_text(
                f"📈 PAPER FORWARD-TEST — {hours} ч.\n"
                f"Измерений: {metrics['samples']}\n"
                f"Капитал: {metrics['start_equity_eur']:.4f} → {metrics['end_equity_eur']:.4f} EUR\n"
                f"Изменение: {metrics['change_eur']:+.4f} EUR ({metrics['change_pct']:+.2f}%)\n"
                f"Макс. наблюдаемая просадка: {metrics['max_observed_drawdown_pct']:.2f}%\n"
                f"Среднее число позиций: {metrics['avg_open_positions']:.2f}\n"
                f"Сигналов BUY: {metrics['buy_signals']}\n"
                f"Среднее число анализируемых рынков за цикл: {metrics['avg_analyzed_markets']:.1f}\n"
                f"Период: {metrics['first_ts']} — {metrics['last_ts']} UTC\n"
                "Это изменение стоимости PAPER-портфеля, а не доказательство будущей доходности."
            )
        except Exception as exc:
            await update.effective_message.reply_text(f"Ошибка отчёта: {safe_error_message(exc)}")

    async def interval(update, context):
        if not await authorized(update): return await deny(update)
        allowed = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "24h": 86400}
        if not context.args or context.args[0] not in allowed:
            return await update.effective_message.reply_text("Использование: /interval 1m|5m|15m|30m|1h|4h|24h\nСканер Render проверяет рынок по заданному интервалу.")
        await context.application.bot_data["run_blocking"].run(
            lambda: setattr(engine, "scan_interval_seconds", allowed[context.args[0]])
        )
        await update.effective_message.reply_text(f"Интервал установлен: {context.args[0]}. Изменение действует до перезапуска сервиса.")

    async def portfolio(update, context):
        if not await authorized(update): return await deny(update)
        def read_portfolio():
            balances = [f"{currency}: {amount:.8f}" for currency, amount in sorted(engine.paper.cash_by_currency.items())]
            fresh_rates = engine._fresh_fx_rates(engine.last_prices)
            required = {
                currency for currency, amount in engine.paper.cash_by_currency.items()
                if float(amount) > 1e-12
            }
            required.update(
                str(position.get("quote_currency", "EUR")).upper()
                for position in engine.paper.positions.values()
            )
            missing = sorted(currency for currency in required if currency not in fresh_rates)
            quotes_fresh, bid_marks = fresh_position_marks()
            lines = []
            for sym, p in engine.paper.positions.items():
                quote = p.get("quote_currency", "EUR")
                mark = bid_marks.get(str(sym).upper()) if quotes_fresh else None
                peak = p.get("peak_price", p["avg_price"])
                if mark is None:
                    lines.append(
                        f"{sym}: {p['qty']:.8f} | вход {p['avg_price']:.8g} {quote} | "
                        "цена недоступна — нет свежей исполнимой котировки | P/L недоступен"
                    )
                    continue
                pnl_quote = (mark - p["avg_price"]) * p["qty"]
                pnl_eur = pnl_quote * fresh_rates.get(quote, 0.0) if quote in fresh_rates else None
                pnl_text = f"{pnl_eur:+.4f} EUR" if pnl_eur is not None else "P/L EUR недоступен"
                lines.append(
                    f"{sym}: {p['qty']:.8f} | вход {p['avg_price']:.8g} {quote} | "
                    f"цена Bid {mark:.8g} | пик {peak:.8g} | {pnl_text}"
                )
            fx_text = "Свежие EUR-курсы для всех валют." if not missing else "Нет свежего EUR-курса: " + ", ".join(missing)
            if not quotes_fresh:
                fx_text += " Нет свежего рыночного снимка; оценка P/L скрыта."
            return "PAPER-кошелёк:\n" + ("\n".join(balances) or "Нет свободных средств.") + "\n" + fx_text + "\n\nПозиции:\n" + ("\n".join(lines) or "Нет открытых позиций.")
        message = await context.application.bot_data["run_blocking"].run(read_portfolio)
        await update.effective_message.reply_text(message)

    async def run(update, context):
        if not await authorized(update): return await deny(update)
        await context.application.bot_data["run_blocking"].run(engine.start)
        await update.effective_message.reply_text("▶️ PAPER-торговля запущена. Автосканирование включено.")

    async def pause(update, context):
        if not await authorized(update): return await deny(update)
        await context.application.bot_data["run_blocking"].run(lambda: engine.stop("Paused by owner"))
        await update.effective_message.reply_text("⏸ Новые сделки остановлены. Открытые PAPER-позиции сохраняются; мониторинг рынка продолжается.")

    async def stop(update, context):
        if not await authorized(update): return await deny(update)
        await context.application.bot_data["run_blocking"].run(lambda: engine.stop("Emergency stop via Telegram"))
        await update.effective_message.reply_text("⛔ STOP: автоматические сделки остановлены.")

    async def topicid(update, context):
        if not await authorized(update): return await deny(update)
        chat = update.effective_chat
        message = update.effective_message
        thread_id = getattr(message, "message_thread_id", None)
        await message.reply_text(
            f"chat_id: {chat.id}\nmessage_thread_id: {thread_id if thread_id is not None else 'нет (личный чат или General)'}"
        )

    async def testtopics(update, context):
        if not await authorized(update):
            return await deny(update)

        message = update.effective_message
        await message.reply_text("Проверяю отправку сообщений во все 5 тем…")

        if not s.telegram_group_chat_id:
            return await message.reply_text("Ошибка настройки: TELEGRAM_GROUP_CHAT_ID не задан в Render.")

        topics = [
            ("Общая информация", s.topic_general_id),
            ("Сделки", s.topic_trades_id),
            ("Баланс и портфель", s.topic_portfolio_id),
            ("Прибыль", s.topic_profit_id),
            ("Ошибки", s.topic_errors_id),
        ]
        results = []
        for title, thread_id in topics:
            if not thread_id:
                results.append(f"❌ {title}: ID темы не задан")
                continue
            try:
                await send_topic(context, thread_id, f"✅ Проверка связи: тема «{title}» настроена.")
                results.append(f"✅ {title}: сообщение отправлено")
            except Exception as exc:
                log.error("Telegram topic test failed: %s (%s)", title, type(exc).__name__)
                results.append(format_error(exc, f"/testtopics — {title}", update.effective_user))

        await message.reply_text("Результат проверки:\n" + "\n".join(results))

    async def help_cmd(update, context):
        if not await authorized(update): return await deny(update)
        await update.effective_message.reply_text(
            "/start — меню\n/status — состояние и P/L\n/scan — сканирование\n"
            "/account — виртуальные остатки PAPER\n/realbalance — реальные остатки Kraken (только чтение)\n"
            "/portfolio — PAPER-позиции\n/paperreport [часы] — статистика автономного PAPER-теста\n"
            "/learn XBTEUR — обучение/проверка ML-кандидата без торговли\n/learning — текущие кандидаты и shadow-тест\n"
            "/research [SYMBOL] — история экспериментов, причины отказа и режимы рынка\n"
            "/backtest XBTEUR — тест одного рынка; /backtest portfolio — до 5 рынков\n"
            "/interval 1m|5m|15m|30m|1h|4h|24h — интервал\n"
            "/run — запуск PAPER\n/pause — пауза\n/stop — аварийная остановка\n"
            "/topicid — ID группы и темы\n/testtopics или /test_topics — проверка тем"
        )

    async def callback(update, context):
        q = update.callback_query
        if not await authorized(update):
            await q.answer("Доступ запрещён", show_alert=True); return
        await q.answer()
        handlers = {"scan": scan, "portfolio": portfolio, "run": run, "pause": pause, "stop": stop}
        fn = handlers.get(q.data)
        if fn: await fn(update, context)

    async def ml_research_job(context):
        """Hourly, bounded, rotating model research across all eligible markets."""
        try:
            def research_top_markets():
                from datetime import datetime, timezone, timedelta
                from .db import get_state, set_state
                from .research_history import perform_research
                now_utc = datetime.now(timezone.utc)
                previous = get_state(engine.db, "ml_auto_research_date", {})
                if isinstance(previous, dict) and previous.get("at_utc"):
                    try:
                        last_run = datetime.fromisoformat(previous["at_utc"])
                        if last_run.tzinfo is None:
                            last_run = last_run.replace(tzinfo=timezone.utc)
                        if now_utc - last_run < timedelta(hours=1):
                            log.info("Automatic ML research skipped: previous batch was less than one hour ago")
                            return []
                    except (TypeError, ValueError):
                        log.warning("Ignoring malformed automatic ML research timestamp")
                pairs = exchange.discover_pairs()
                tickers = exchange.tickers()
                engine.market_pairs = pairs
                by_symbol = {item["symbol"]: item for item in tickers}
                engine.last_ticker_data = by_symbol
                prices = {
                    item["symbol"]: float(item.get("lastPrice") or 0)
                    for item in tickers if float(item.get("lastPrice") or 0) > 0
                }
                rates = engine._fresh_fx_rates(prices)
                eligible = []
                for pair in pairs:
                    ticker = by_symbol.get(pair["symbol"])
                    quote = str(pair.get("quote") or "").upper()
                    rate = rates.get(quote, 0.0)
                    if not ticker or rate <= 0:
                        continue
                    bid, ask = float(ticker.get("bid1Price") or 0), float(ticker.get("ask1Price") or 0)
                    spread = (ask - bid) / ((ask + bid) / 2) * 10000 if bid > 0 and ask > 0 else 999999
                    turnover_eur = float(ticker.get("turnover24h") or 0) * rate
                    if spread <= s.max_spread_bps and turnover_eur >= s.min_turnover:
                        eligible.append((turnover_eur, pair["symbol"]))
                eligible.sort(reverse=True)
                log.info("Automatic ML research discovered %d Kraken pairs; %d pass liquidity/FX filters",
                         len(pairs), len(eligible))
                from .learning import rotate_research_batch
                cursor = get_state(engine.db, "ml_auto_research_cursor", 0)
                selected, next_cursor = rotate_research_batch(
                    eligible, cursor, s.ml_auto_research_markets
                )
                if selected:
                    set_state(engine.db, "ml_auto_research_cursor", next_cursor)
                log.info("Automatic ML research selected %d markets for this batch", len(selected))
                trained = []
                for _, symbol in selected:
                    try:
                        candidate = perform_research(
                            engine.db, exchange, s, symbol, source="automatic"
                        )
                        trained.append({
                            "symbol": symbol,
                            "pass": bool(candidate["research_pass"]),
                            "samples": candidate["samples"],
                            "version": candidate["model_version"],
                            "reasons": candidate.get("rejection_reasons", []),
                            "oos_return": candidate["out_of_sample"]["net_return_pct"],
                            "holdout_return": candidate["independent_holdout"]["net_return_pct"],
                            "benchmarks": candidate.get("benchmark_metrics", {}),
                            "auc_oos": candidate.get("score_ranking", {}).get("walk_forward_oos_roc_auc"),
                            "auc_holdout": candidate.get("score_ranking", {}).get("independent_holdout_roc_auc"),
                        })
                    except Exception as market_exc:
                        log.warning("Automatic model research skipped %s: %s", symbol, market_exc)
                log.info("Automatic ML research completed: %d of %d selected markets trained",
                         len(trained), len(selected))
                if trained:
                    set_state(engine.db, "ml_auto_research_date", {
                        "at_utc": now_utc.isoformat(),
                        "markets": trained,
                        "cadence_hours": 1,
                    })
                return trained

            trained = await context.application.bot_data["run_blocking"].run(research_top_markets)
            if trained:
                summary = "\n".join(
                    f"{item['symbol']}: {'research PASS' if item['pass'] else 'NOT PASSED'} "
                    f"({item['samples']} samples; OOS {item['oos_return']:+.2f}%; "
                    f"holdout {item['holdout_return']:+.2f}%; "
                    f"BUY&HOLD OOS {item['benchmarks'].get('walk_forward_oos', {}).get('always_long', {}).get('net_return_pct', 0):+.2f}%; "
                    f"BUY&HOLD holdout {item['benchmarks'].get('independent_holdout', {}).get('always_long', {}).get('net_return_pct', 0):+.2f}%; "
                    f"AUC {item['auc_oos'] if item['auc_oos'] is not None else 'n/a'}; shadow only)"
                    + (("\nПричина: " + "; ".join(item["reasons"][:2])) if item["reasons"] else "")
                    for item in trained
                )
                await send_topic(context, s.topic_general_id, "🧠 Плановое исследование моделей завершено.\n" + summary)
        except Exception as exc:
            log.error("Automatic periodic ML research failed: %s", type(exc).__name__)
            try:
                await send_topic(context, s.topic_errors_id, f"Ошибка планового ML-исследования: {safe_error_message(exc)}")
            except Exception:
                log.error("Could not send ML research error to Telegram")

    async def _cycle_job(context):
        import time
        now = time.monotonic()
        last = context.application.bot_data.get("last_cycle_at", 0.0)
        if now - last < engine.scan_interval_seconds:
            return
        context.application.bot_data["last_cycle_at"] = now
        runner = context.application.bot_data["run_blocking"]

        # An unexpected engine/database exception is a safety fault: pause
        # PAPER before reporting it. Telegram notification failures alone do
        # not stop an otherwise healthy trading engine.
        try:
            actions = await runner.run(engine.cycle)
        except Exception as exc:
            try:
                await runner.run(lambda: engine.stop("Unexpected trading cycle exception"))
            except Exception as stop_exc:
                log.error("Could not pause after cycle failure: %s", type(stop_exc).__name__)
            log.error("Trading cycle failed: %s", type(exc).__name__)
            try:
                await send_topic(context, s.topic_errors_id, format_error(exc, "автоматический торговый цикл"), parse_mode="HTML")
            except Exception:
                log.error("Could not send error notification to Telegram topic")
            return

        def read_cycle_state():
            fresh_rates = engine._fresh_fx_rates(engine.last_prices)
            required_currencies = {
                currency for currency, amount in engine.paper.cash_by_currency.items()
                if float(amount) > 1e-12
            } | {
                str(position.get("quote_currency", "EUR")).upper()
                for position in engine.paper.positions.values()
            }
            missing_fx = sorted(currency for currency in required_currencies if currency not in fresh_rates)
            return {
                "paused": engine.paused,
                "last_scan_at": engine.last_scan_at,
                "last_signals": list(engine.last_signals),
                "cash": dict(engine.paper.cash_by_currency),
                "positions": {symbol: dict(position) for symbol, position in engine.paper.positions.items()},
                "daily_pnl": engine.daily_pnl(engine.last_prices) if not missing_fx else None,
                "missing_fx": missing_fx,
            }

        try:
            state = await runner.run(read_cycle_state)
            daily_text = f"{state['daily_pnl']:.2f} EUR" if state["daily_pnl"] is not None else "недоступен — нет свежего FX: " + ", ".join(state["missing_fx"])

            # Send a compact heartbeat to General every 15 minutes after a
            # successful scan while PAPER is actively trading.
            last_report = context.application.bot_data.get("last_general_report_at", 0.0)
            if state["last_scan_at"] and not state["paused"] and now - last_report >= 900:
                scan_time = state["last_scan_at"].strftime("%Y-%m-%d %H:%M:%S UTC")
                signal_lines = [
                    f"{item['symbol']}: {item.get('signal', 'N/A')} ({item.get('score', 0)}/5) — {float(item.get('price') or 0):,.6g}\n"
                    f"Причина: {'; '.join(item.get('reasons', [])[:2]) or 'нет'}"
                    for item in sorted(state["last_signals"], key=lambda item: item.get("score", 0), reverse=True)[:15]
                ]
                position_lines = [
                    f"{symbol}: {position['qty']:.8f} @ {position['avg_price']:.8g} {position.get('quote_currency', 'EUR')} | peak {position.get('peak_price', position['avg_price']):.8g}"
                    for symbol, position in state["positions"].items()
                ]
                report = (
                    "🟢 PAPER — автоматический отчёт\n"
                    f"Последнее успешное сканирование: {scan_time}\n"
                    f"Виртуальные средства: {state['cash']}\n"
                    f"Открытые позиции: {len(state['positions'])}/{s.max_positions}\n"
                    f"P/L сегодня: {daily_text}\n"
                    "Рыночные сигналы:\n" + ("\n".join(signal_lines) or "Нет данных") +
                    "\nПозиции:\n" + ("\n".join(position_lines) or "Нет открытых позиций")
                )
                await send_topic(context, s.topic_general_id, report)
                context.application.bot_data["last_general_report_at"] = now

            if actions:
                error_actions = [item for item in actions if "SCAN_FAILED" in item or "ERROR" in item or "FX_ROUTE_MISSING" in item]
                trade_actions = [item for item in actions if " BUY " in item or " SELL " in item]
                profit_actions = [item for item in actions if any(token in item for token in (" SELL ", "STOP_LOSS", "TAKE_PROFIT", "TRAILING_STOP", "DAILY LOSS"))]

                if error_actions:
                    await send_topic(context, s.topic_errors_id, "⚠️ Ошибка рыночного сканирования/оценки:\n" + "\n".join(error_actions))
                    await send_topic(context, s.topic_general_id, "⏸ PAPER автоматически приостановлен из-за ошибки рыночных данных или валютной оценки.")

                if trade_actions:
                    trade_message = "📈 PAPER — сделки:\n" + "\n".join(trade_actions)
                    await send_topic(context, s.topic_trades_id, trade_message)
                    portfolio_message = (
                        "💼 PAPER — обновление портфеля\n"
                        f"Доступно: {state['cash']}\n"
                        f"Открытых позиций: {len(state['positions'])}/{s.max_positions}\n"
                        f"P/L сегодня: {daily_text}"
                    )
                    await send_topic(context, s.topic_portfolio_id, portfolio_message)

                if profit_actions:
                    await send_topic(context, s.topic_profit_id, "💰 PAPER — результат/риск:\n" + "\n".join(profit_actions))

                if any("DAILY LOSS" in item for item in actions):
                    await send_topic(context, s.topic_general_id, "⏸ Достигнут дневной лимит убытка. PAPER остановлен.")
        except Exception as exc:
            # A Telegram outage must not convert a successful PAPER cycle into
            # an engine fault. Report the notification error without stopping.
            log.error("Trading cycle notification failed: %s", type(exc).__name__)
            try:
                await send_topic(context, s.topic_errors_id, f"Ошибка уведомления: {safe_error_message(exc)}")
            except Exception:
                log.error("Could not send notification failure to Telegram topic")

    async def cycle_job(context):
        """Run one scan, then schedule the next after completion (fixed-delay)."""
        import time
        started = time.monotonic()
        try:
            await _cycle_job(context)
        finally:
            duration = time.monotonic() - started
            target_interval = float(engine.scan_interval_seconds)
            delay = max(0.0, target_interval - duration)
            log.info(
                "Trading cycle finished in %.1fs; next scan scheduled in %.1fs",
                duration,
                delay,
            )
            # Schedule only after this run finishes. Keep the configured
            # start-to-start cadence when possible, but never overlap or drop
            # a scan if the previous cycle or serialized ML research runs long.
            context.job_queue.run_once(
                cycle_job,
                when=delay,
                name="paper-cycle",
            )

    app = Application.builder().token(s.bot_token).build()
    # Serialize all exchange/SQLite work: cycle jobs, scans and backtests share
    # one requests.Session, engine state and SQLite connection.
    from .worker import SerializedRunner
    app.bot_data["run_blocking"] = SerializedRunner()
    for name, fn in [("start", start), ("status", status), ("scan", scan), ("account", account), ("realbalance", realbalance), ("paperreport", paperreport), ("learn", learn), ("learning", learning), ("research", research_history_cmd), ("backtest", backtest), ("interval", interval), ("portfolio", portfolio), ("topicid", topicid), ("testtopics", testtopics),
                     ("run", run), ("pause", pause), ("stop", stop), ("help", help_cmd)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(callback))

    async def application_error_handler(update, context):
        error = context.error
        # Do not log exception strings: HTTP request exceptions can contain
        # credential-bearing request URLs. Keep only the exception class.
        error_name = type(error).__name__ if error else "UnknownError"
        log.error("Unhandled Telegram application error: %s", error_name)
        try:
            await send_topic(
                context, s.topic_errors_id,
                format_error(error, "обработчик Telegram", getattr(update, "effective_user", None), source="Telegram"),
                parse_mode="HTML",
            )
        except Exception:
            log.error("Could not notify Telegram error topic")

    app.add_error_handler(application_error_handler)
    # Start once, then cycle_job schedules the next scan after completion.
    # This fixed-delay cadence avoids overlapping jobs and skipped scans.
    app.job_queue.run_once(cycle_job, when=10, name="paper-cycle")
    app.job_queue.run_repeating(ml_research_job, interval=3600, first=60, name="hourly-ml-research")
    return app
