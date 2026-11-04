import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

log = logging.getLogger(__name__)

def build_app(s, engine, exchange):
    def format_error(exc, action, user=None):
        import traceback
        if user:
            username = f"@{user.username}" if user.username else "без username"
            who = f"{user.id} ({username})"
        else:
            who = "— (—)"
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        return (
            "🚨 ОШИБКА CRYPTO AI TRADER\n"
            f"👤 Кто: {who}\n"
            f"💬 Действие: {action}\n"
            f"⚠️ Тип: {type(exc).__name__}\n"
            f"📝 Описание: {str(exc) or 'Без дополнительного описания'}\n"
            "📋 Трейс:\n"
            + '```python\n' + trace[-2200:] + '\n```'
        )[:3900]
    async def send_topic(context, topic_id, text):
        if not s.telegram_group_chat_id:
            return
        kwargs = {"chat_id": s.telegram_group_chat_id, "text": text[:3900]}
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
        state = "PAUSED" if engine.paused else "RUNNING"
        balances = ", ".join(f"{currency}: {amount:.6f}" for currency, amount in sorted(engine.paper.cash_by_currency.items()))
        await update.effective_message.reply_text(
            f"Режим: PAPER\nСостояние: {state}\nВиртуальные средства: {balances}\n"
            f"Позиции: {len(engine.paper.positions)}/{s.max_positions}\nP/L сегодня: {engine.daily_pnl():.2f} EUR*\n"
            f"Риск: SL {s.stop_loss_pct:.2f}% | TP {s.take_profit_pct:.2f}% | trailing {getattr(s, 'trailing_stop_pct', 1.5):.2f}%\n"
            f"Рынок Kraken: {len(engine.market_pairs)} пар; котировок получено: {engine.market_ticker_count}\n"
            f"Мониторинг всех котировок; анализ свечей: до {s.max_candle_pairs} пар за цикл.\n"
            "* P/L рассчитан в EUR с учётом доступных валютных курсов Kraken."
        )

    async def scan(update, context):
        if not await authorized(update): return await deny(update)
        await update.effective_message.reply_text("Сканирование рынка…")
        try:
            rows = await context.application.bot_data["run_blocking"].run(engine.scan)
            buy_rows = [x for x in rows if x.get("signal") == "BUY"]
            scan_prices = {
                x["symbol"]: float(x.get("price") or 0)
                for x in rows if x.get("symbol") and float(x.get("price") or 0) > 0
            }
            fx_rates = engine._fx_rates(scan_prices)
            analyzed = [
                x for x in rows
                if "Ticker monitored; OHLC analysis deferred by rotating scan budget" not in x.get("reasons", [])
            ]
            ranked = sorted(
                analyzed,
                key=lambda x: (x.get("score", 0), x.get("turnover_eur", 0)),
                reverse=True,
            )[:12]
            lines = [
                f"Всего рынков Kraken: {len(engine.market_pairs)}",
                f"Тикеров получено: {engine.market_ticker_count}",
                f"Пар с доступным EUR-маршрутом, прошедших анализ свечей: {len(analyzed)}",
                f"Сигналов BUY: {len(buy_rows)}",
                "",
            ]
            lines.extend(
                f"{x['symbol']}: {x['signal']} ({x.get('score', 0)}/5), "
                f"оборот {x.get('turnover_eur', 0):,.0f} EUR/24ч, цена {x.get('price', 0):,.8g}"
                for x in ranked
            )
            lines.append("Все рынки получают мониторинг котировок; анализ свечей ограничен бюджетом запросов.")
            await update.effective_message.reply_text("\n".join(lines)[:3900])
        except Exception as exc:
            # Keep the full traceback in Render logs and expose the actionable
            # exception type/message in the private owner Telegram response.
            log.exception("Market scan failed")
            detail = f"{type(exc).__name__}: {exc}"
            await update.effective_message.reply_text(
                format_error(exc, "/scan", update.effective_user)
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
                    prices = {x["symbol"]: float(x.get("lastPrice") or 0) for x in tickers if float(x.get("lastPrice") or 0) > 0}
                    fx_rates = engine._fx_rates(prices)
                    by_symbol = {x["symbol"]: x for x in tickers}
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
                            for row in fx_rows:
                                if row[0] not in common:
                                    continue
                                dated_fx_by_timestamp.setdefault(row[0], {"EUR": 1.0})[currency] = (
                                    row[4] if direct else 1.0 / row[4]
                                )
                        except Exception:
                            log.exception("Historical FX candles unavailable for %s", currency)
                            fx_fallback.append(currency)
                    result = run_portfolio_backtest(
                        candles_by_symbol,
                        quote_currencies=quote_currencies,
                        fx_rates=fx_rates,
                        fx_rates_by_timestamp=dated_fx_by_timestamp,
                        initial_cash_eur=s.paper_start,
                        max_trade_eur=s.max_trade,
                        max_positions=s.max_positions,
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
                log.exception("Portfolio historical backtest failed")
                await update.effective_message.reply_text(format_error(exc, "/backtest portfolio", update.effective_user))
            return
        await update.effective_message.reply_text(f"Исторический тест {symbol}…")
        try:
            from .backtest import run_backtest
            result = await context.application.bot_data["run_blocking"].run(
                lambda: run_backtest(
                    exchange.candles(symbol, interval="60", limit=720),
                    initial_cash=s.paper_start,
                    max_trade=s.max_trade,
                    fee_rate=s.fee_rate,
                    slippage_bps=5.0,
                    stop_loss_pct=s.stop_loss_pct,
                    take_profit_pct=s.take_profit_pct,
                    trailing_stop_pct=s.trailing_stop_pct,
                )
            )
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
                f"Комиссии: {result['fees_paid']:.4f}\n"
                "Издержки: комиссия на вход/выход + проскальзывание 5 bps на сторону. "
                "Исполнение моделируется по открытию следующей свечи. "
                "Это тест одного рынка, не прогноз и не гарантия будущей прибыли."
            )
        except Exception as exc:
            log.exception("Historical backtest failed for %s", symbol)
            await update.effective_message.reply_text(
                format_error(exc, f"/backtest {symbol}", update.effective_user)
            )

    async def account(update, context):
        if not await authorized(update): return await deny(update)
        balances = [f"{currency}: {amount:.8f}" for currency, amount in sorted(engine.paper.cash_by_currency.items())]
        await update.effective_message.reply_text("Виртуальные остатки PAPER:\n" + ("\n".join(balances) or "Нулевые остатки."))

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
            log.exception("Read-only Kraken balance request failed")
            await update.effective_message.reply_text(format_error(exc, "/realbalance", update.effective_user))

    async def learn(update, context):
        if not await authorized(update): return await deny(update)
        if len(context.args) != 1:
            return await update.effective_message.reply_text("Использование: /learn XBTEUR — исследование модели на закрытых часовых свечах.")
        symbol = context.args[0].strip().upper().replace("/", "")
        await update.effective_message.reply_text(
            f"🧠 Исследую {symbol}: обучаю модель и провожу 3 хронологических walk-forward проверки. "
            "Это не запускает и не меняет торговлю."
        )
        try:
            def run_research():
                from .learning import research_candidate
                from .db import set_state
                candles = exchange.candles(symbol, interval="60", limit=720)
                candidate = research_candidate(candles, fee_rate=s.fee_rate, slippage_bps=5.0)
                candidate["symbol"] = symbol
                candidate["trained_at_utc"] = __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat()
                set_state(engine.db, "ml_candidate_" + symbol, candidate)
                return candidate
            candidate = await context.application.bot_data["run_blocking"].run(run_research)
            metrics = candidate["out_of_sample"]
            pf = metrics["profit_factor"]
            pf_text = "∞" if pf == "INF" else f"{pf:.2f}"
            gate = "ПРОШЛА исследовательский фильтр" if candidate["research_pass"] else "НЕ прошла исследовательский фильтр"
            await update.effective_message.reply_text(
                f"🧠 МОДЕЛЬ {symbol} — {gate}\n"
                f"Статус: SHADOW ONLY (не торгует)\n"
                f"Обучающих/тестовых выборок: {candidate['samples']}\n"
                f"Walk-forward фолдов: {len(candidate['walk_forward'])}/3\n"
                f"Вневыборочных сделок: {metrics['trades']}\n"
                f"Доходность после издержек: {metrics['net_return_pct']:+.2f}%\n"
                f"Win rate: {metrics['win_rate_pct']:.1f}% | Profit factor: {pf_text}\n"
                f"Макс. просадка: {metrics['max_drawdown_pct']:.2f}%\n"
                "Модель сохранена как исследовательский кандидат. Автоматического внедрения нет; "
                "следующий этап — длительное теневое сравнение с базовой стратегией."
            )
        except Exception as exc:
            log.exception("ML research failed for %s", symbol)
            await update.effective_message.reply_text(format_error(exc, f"/learn {symbol}", update.effective_user))

    async def learning(update, context):
        if not await authorized(update): return await deny(update)
        from .db import get_state, get_shadow_metrics
        rows = engine.db.execute(
            "SELECT key FROM app_state WHERE key LIKE 'ml_candidate_%' ORDER BY updated_at DESC"
        ).fetchall()
        if not rows:
            return await update.effective_message.reply_text("Исследовательских моделей пока нет. Запусти /learn XBTEUR.")
        lines = ["🧠 ИССЛЕДОВАТЕЛЬСКИЕ МОДЕЛИ (не торгуют):"]
        for row in rows[:15]:
            item = get_state(engine.db, row["key"], {})
            metric = item.get("out_of_sample", {})
            symbol = item.get("symbol", row["key"].replace("ml_candidate_", ""))
            shadow = get_shadow_metrics(engine.db, symbol)
            lines.append(
                f"{symbol}: {'PASS' if item.get('research_pass') else 'NOT PASS'} | "
                f"{metric.get('net_return_pct', 0):+.2f}% OOS | "
                f"PF {metric.get('profit_factor', 0)} | "
                f"SHADOW {shadow['shadow_trades']} trades / {shadow['resolved_predictions']} resolved | "
                f"{shadow['net_return_pct']:+.2f}% | SHADOW ONLY"
            )
        await update.effective_message.reply_text("\n".join(lines)[:3900])

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
            await update.effective_message.reply_text(f"Ошибка отчёта: {type(exc).__name__}: {exc}")

    async def interval(update, context):
        if not await authorized(update): return await deny(update)
        allowed = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "24h": 86400}
        if not context.args or context.args[0] not in allowed:
            return await update.effective_message.reply_text("Использование: /interval 1m|5m|15m|30m|1h|4h|24h\nСканер Render проверяет рынок по заданному интервалу.")
        engine.scan_interval_seconds = allowed[context.args[0]]
        await update.effective_message.reply_text(f"Интервал установлен: {context.args[0]}. Изменение действует до перезапуска сервиса.")

    async def portfolio(update, context):
        if not await authorized(update): return await deny(update)
        balances = [f"{currency}: {amount:.8f}" for currency, amount in sorted(engine.paper.cash_by_currency.items())]
        fx_rates = engine._fx_rates(engine.last_prices)
        lines = []
        for sym, p in engine.paper.positions.items():
            quote = p.get("quote_currency", "EUR")
            mark = engine.last_prices.get(sym, p["avg_price"])
            pnl_quote = (mark - p["avg_price"]) * p["qty"]
            pnl_eur = pnl_quote * fx_rates.get(quote, 0.0)
            peak = p.get("peak_price", p["avg_price"])
            lines.append(
                f"{sym}: {p['qty']:.8f} | вход {p['avg_price']:.8g} {quote} | "
                f"цена {mark:.8g} | пик {peak:.8g} | P/L {pnl_eur:+.4f} EUR"
            )
        await update.effective_message.reply_text("PAPER-кошелёк:\n" + ("\n".join(balances) or "Нет свободных средств.") + "\n\nПозиции:\n" + ("\n".join(lines) or "Нет открытых позиций."))

    async def run(update, context):
        if not await authorized(update): return await deny(update)
        engine.start()
        await update.effective_message.reply_text("▶️ PAPER-торговля запущена. Автосканирование включено.")

    async def pause(update, context):
        if not await authorized(update): return await deny(update)
        engine.stop("Paused by owner")
        await update.effective_message.reply_text("⏸ Новые сделки остановлены. Открытые PAPER-позиции сохраняются.")

    async def stop(update, context):
        if not await authorized(update): return await deny(update)
        engine.stop("Emergency stop via Telegram")
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
                log.exception("Telegram topic test failed: %s", title)
                results.append(format_error(exc, f"/testtopics — {title}", update.effective_user))

        await message.reply_text("Результат проверки:\n" + "\n".join(results))

    async def help_cmd(update, context):
        if not await authorized(update): return await deny(update)
        await update.effective_message.reply_text("/start — меню\n/status — состояние и P/L\n/scan — сканирование\n/account — виртуальные остатки PAPER\n/portfolio — PAPER-позиции\n/backtest XBTEUR — тест одного рынка; /backtest portfolio — до 5 рынков\n/interval 1m|5m|15m|30m|1h|4h|24h — интервал\n/run — запуск PAPER\n/pause — пауза\n/stop — аварийная остановка\n/topicid — ID группы и темы\n/testtopics или /test_topics — проверка тем")

    async def callback(update, context):
        q = update.callback_query
        if not await authorized(update):
            await q.answer("Доступ запрещён", show_alert=True); return
        await q.answer()
        handlers = {"scan": scan, "portfolio": portfolio, "run": run, "pause": pause, "stop": stop}
        fn = handlers.get(q.data)
        if fn: await fn(update, context)

    async def cycle_job(context):
        import time
        now = time.monotonic()
        last = context.application.bot_data.get("last_cycle_at", 0.0)
        if now - last < engine.scan_interval_seconds:
            return
        context.application.bot_data["last_cycle_at"] = now
        try:
            actions = await context.application.bot_data["run_blocking"].run(engine.cycle)

            # Send a compact heartbeat to General every 15 minutes, but only
            # after a successful automatic market scan while PAPER is running.
            last_report = context.application.bot_data.get("last_general_report_at", 0.0)
            if engine.last_scan_at and not engine.paused and now - last_report >= 900:
                scan_time = engine.last_scan_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                signal_lines = [
                    f"{item['symbol']}: {item.get('signal', 'N/A')} ({item.get('score', 0)}/5) — {float(item.get('price') or 0):,.6g}"
                    for item in sorted(engine.last_signals, key=lambda item: item.get("score", 0), reverse=True)[:15]
                ]
                position_lines = [
                    f"{symbol}: {position['qty']:.8f} @ {position['avg_price']:.8g} {position.get('quote_currency', 'EUR')} | peak {position.get('peak_price', position['avg_price']):.8g}"
                    for symbol, position in engine.paper.positions.items()
                ]
                report = (
                    "🟢 PAPER — автоматический отчёт\n"
                    f"Последнее успешное сканирование: {scan_time}\n"
                    f"Виртуальные средства: {engine.paper.cash_by_currency}\n"
                    f"Открытые позиции: {len(engine.paper.positions)}/{s.max_positions}\n"
                    f"P/L сегодня: {engine.daily_pnl(engine.last_prices):.2f} EUR\n"
                    "Рыночные сигналы:\n" + ("\n".join(signal_lines) or "Нет данных") +
                    "\nПозиции:\n" + ("\n".join(position_lines) or "Нет открытых позиций")
                )
                await send_topic(context, s.topic_general_id, report)
                context.application.bot_data["last_general_report_at"] = now

            if actions:
                error_actions = [item for item in actions if "SCAN_FAILED" in item or "ERROR" in item]
                trade_actions = [item for item in actions if " BUY " in item or " SELL " in item]
                profit_actions = [item for item in actions if any(token in item for token in (" SELL ", "STOP_LOSS", "TAKE_PROFIT", "TRAILING_STOP", "DAILY LOSS"))]

                if error_actions:
                    await send_topic(context, s.topic_errors_id, "⚠️ Ошибка рыночного сканирования:\n" + "\n".join(error_actions))
                    await send_topic(context, s.topic_general_id, "⏸ PAPER автоматически приостановлен из-за ошибки рыночных данных.")

                if trade_actions:
                    trade_message = "📈 PAPER — сделки:\n" + "\n".join(trade_actions)
                    await send_topic(context, s.topic_trades_id, trade_message)
                    positions = len(engine.paper.positions)
                    portfolio_message = (
                        "💼 PAPER — обновление портфеля\n"
                        f"Доступно: {engine.paper.cash_by_currency}\n"
                        f"Открытых позиций: {positions}/{s.max_positions}\n"
                        f"P/L сегодня: {engine.daily_pnl():.2f} EUR"
                    )
                    await send_topic(context, s.topic_portfolio_id, portfolio_message)

                if profit_actions:
                    await send_topic(context, s.topic_profit_id, "💰 PAPER — результат/риск:\n" + "\n".join(profit_actions))

                if any("DAILY LOSS" in item for item in actions):
                    await send_topic(context, s.topic_general_id, "⏸ Достигнут дневной лимит убытка. PAPER остановлен.")
        except Exception as exc:
            log.exception("Trading cycle failed")
            try:
                await send_topic(context, s.topic_errors_id, format_error(exc, "автоматический торговый цикл"))
            except Exception:
                log.exception("Could not send error notification to Telegram topic")

    app = Application.builder().token(s.bot_token).build()
    # Serialize all exchange/SQLite work: cycle jobs, scans and backtests share
    # one requests.Session, engine state and SQLite connection.
    from .worker import SerializedRunner
    app.bot_data["run_blocking"] = SerializedRunner()
    for name, fn in [("start", start), ("status", status), ("scan", scan), ("account", account), ("realbalance", realbalance), ("paperreport", paperreport), ("learn", learn), ("learning", learning), ("backtest", backtest), ("interval", interval), ("portfolio", portfolio), ("topicid", topicid), ("testtopics", testtopics),
                     ("run", run), ("pause", pause), ("stop", stop), ("help", help_cmd)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(callback))
    app.job_queue.run_repeating(cycle_job, interval=1, first=10, name="paper-cycle")
    return app
