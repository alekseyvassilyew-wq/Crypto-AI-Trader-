import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

log = logging.getLogger(__name__)

def build_app(s, engine, exchange):
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
        await update.effective_message.reply_text(f"Режим: PAPER\nСостояние: {state}\nPAPER cash: {engine.paper.cash:.2f} EUR\nПозиции: {len(engine.paper.positions)}/{s.max_positions}\nP/L сегодня: {engine.daily_pnl():.2f} EUR\nПары: {', '.join(s.watchlist)}")

    async def scan(update, context):
        if not await authorized(update): return await deny(update)
        await update.effective_message.reply_text("Сканирование рынка…")
        try:
            rows = await context.application.bot_data["run_blocking"].run(engine.scan)
            lines = [f"{x['symbol']}: {x['signal']} ({x.get('score', 0)}/5), цена {x.get('price', 0)}" for x in rows]
            await update.effective_message.reply_text("\n".join(lines)[:3900])
        except Exception:
            log.exception("Market scan failed")
            await update.effective_message.reply_text("Ошибка сканирования. Подробности в логах Render.")

    async def account(update, context):
        if not await authorized(update): return await deny(update)
        try:
            balances = await context.application.bot_data["run_blocking"].run(exchange.balance)
            nonzero = [f"{asset}: {float(amount):.8f}" for asset, amount in balances.items() if float(amount) > 0]
            await update.effective_message.reply_text("Kraken account balances (read-only):\n" + ("\n".join(nonzero[:30]) or "Нулевые остатки."))
        except Exception as exc:
            log.exception("Kraken account query failed")
            await update.effective_message.reply_text(f"Не удалось прочитать баланс Kraken: {str(exc)[:500]}")

    async def interval(update, context):
        if not await authorized(update): return await deny(update)
        allowed = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "24h": 86400}
        if not context.args or context.args[0] not in allowed:
            return await update.effective_message.reply_text("Использование: /interval 1m|5m|15m|30m|1h|4h|24h\nСканер Render проверяет рынок по заданному интервалу.")
        engine.scan_interval_seconds = allowed[context.args[0]]
        await update.effective_message.reply_text(f"Интервал установлен: {context.args[0]}. Изменение действует до перезапуска сервиса.")

    async def portfolio(update, context):
        if not await authorized(update): return await deny(update)
        if not engine.paper.positions: return await update.effective_message.reply_text(f"Открытых позиций нет. PAPER cash: {engine.paper.cash:.2f} EUR")
        lines = [f"{sym}: {p['qty']:.8f} @ avg {p['avg_price']:.6f}" for sym, p in engine.paper.positions.items()]
        await update.effective_message.reply_text(f"Cash: {engine.paper.cash:.2f} EUR\n" + "\n".join(lines))

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

    async def help_cmd(update, context):
        if not await authorized(update): return await deny(update)
        await update.effective_message.reply_text("/start — меню\n/status — состояние и P/L\n/scan — сканирование\n/account — баланс Kraken (только чтение)\n/portfolio — PAPER-позиции\n/interval 1m|5m|15m|30m|1h|4h|24h — интервал\n/run — запуск PAPER\n/pause — пауза\n/stop — аварийная остановка\n/topicid — ID группы и темы")

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
            if actions:
                await context.bot.send_message(chat_id=s.admin_id, text="PAPER cycle:\n" + "\n".join(actions)[:3500])
        except Exception:
            log.exception("Trading cycle failed")

    app = Application.builder().token(s.bot_token).build()
    # Run synchronous exchange/SQLite work in a worker thread to keep Telegram responsive.
    import asyncio
    class Runner:
        @staticmethod
        async def run(fn): return await asyncio.to_thread(fn)
    app.bot_data["run_blocking"] = Runner
    for name, fn in [("start", start), ("status", status), ("scan", scan), ("account", account), ("interval", interval), ("portfolio", portfolio), ("topicid", topicid),
                     ("run", run), ("pause", pause), ("stop", stop), ("help", help_cmd)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(callback))
    app.job_queue.run_repeating(cycle_job, interval=1, first=10, name="paper-cycle")
    return app
