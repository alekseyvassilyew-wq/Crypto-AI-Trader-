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

    async def account(update, context):
        if not await authorized(update): return await deny(update)
        balances = [f"{currency}: {amount:.8f}" for currency, amount in sorted(engine.paper.cash_by_currency.items())]
        await update.effective_message.reply_text("Виртуальные остатки PAPER:\n" + ("\n".join(balances) or "Нулевые остатки."))

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
        await update.effective_message.reply_text("/start — меню\n/status — состояние и P/L\n/scan — сканирование\n/account — виртуальные остатки PAPER\n/portfolio — PAPER-позиции\n/interval 1m|5m|15m|30m|1h|4h|24h — интервал\n/run — запуск PAPER\n/pause — пауза\n/stop — аварийная остановка\n/topicid — ID группы и темы\n/testtopics или /test_topics — проверка тем")

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
                    f"{symbol}: {position['qty']:.8f} @ {position['avg_price']:.8g} {position.get('quote_currency', 'EUR')}"
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
    # Run synchronous exchange/SQLite work in a worker thread to keep Telegram responsive.
    import asyncio
    class Runner:
        @staticmethod
        async def run(fn): return await asyncio.to_thread(fn)
    app.bot_data["run_blocking"] = Runner
    for name, fn in [("start", start), ("status", status), ("scan", scan), ("account", account), ("interval", interval), ("portfolio", portfolio), ("topicid", topicid), ("testtopics", testtopics),
                     ("run", run), ("pause", pause), ("stop", stop), ("help", help_cmd)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(callback))
    app.job_queue.run_repeating(cycle_job, interval=1, first=10, name="paper-cycle")
    return app
