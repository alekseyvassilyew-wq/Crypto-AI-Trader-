import logging
import html
from datetime import datetime, timezone
from pathlib import Path
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.request import HTTPXRequest
from .db import backup_database

log = logging.getLogger(__name__)

def _format_scan_report(rows, pair_count, ticker_count):
    """Render /scan in the same plain-text, line-by-line style as /monitorstatus."""
    def number(value, digits=2):
        try:
            return f"{float(value):,.{digits}f}"
        except (TypeError, ValueError, OverflowError):
            return "н/д"

    buy_rows = [item for item in rows if item.get("signal") == "BUY"]
    analyzed = [item for item in rows if item.get("ohlc_analyzed") is True]
    ranked = sorted(
        analyzed,
        key=lambda item: (item.get("score", 0), item.get("turnover_eur", 0)),
        reverse=True,
    )[:12]
    lines = [
        "🔎 СКАНИРОВАНИЕ РЫНКА KRAKEN",
        "━━━━━━━━━━━━━━━━━━",
        "🌐 СТАТИСТИКА РЫНКА",
        f"Всего рынков Kraken: {int(pair_count):,}",
        f"Тикеров получено: {int(ticker_count):,}",
        f"Пар с доступным EUR-маршрутом, прошедших анализ свечей: {len(analyzed)}",
        f"Сигналов BUY: {len(buy_rows)}",
        "",
        "📊 РЕЗУЛЬТАТЫ АНАЛИЗА",
    ]
    for item in ranked:
        symbol = str(item.get("symbol") or "—")
        signal = str(item.get("signal") or "HOLD").upper()
        score = number(item.get("score", 0), 0)
        turnover = number(item.get("turnover_eur", 0), 0)
        price = number(item.get("price"), 8)
        lines.append(
            f"{symbol}: {signal} ({score}/5), оборот {turnover} EUR/24ч, цена {price}"
        )
        reasons = item.get("reasons") or []
        if reasons:
            lines.append("  Причина: " + "; ".join(str(reason) for reason in reasons[:2]))
    if not ranked:
        lines.append("Нет пар, прошедших анализ свечей в этом цикле.")
    lines.extend([
        "",
        "Все рынки получают мониторинг котировок; анализ свечей ограничен бюджетом запросов.",
        "",
        "━━━━━━━━━━━━━━━━━━",
        "🪙 ОТДЕЛЬНАЯ ПРОВЕРКА TON",
    ])
    ton = {
        str(item.get("symbol") or "").upper(): item
        for item in rows
        if str(item.get("symbol") or "").upper() in {"TONEUR", "TONUSD"}
    }
    if ton:
        for symbol in ("TONEUR", "TONUSD"):
            item = ton.get(symbol)
            if item is None:
                continue
            spread = item.get("spread_bps")
            spread_text = f"{number(spread, 1)} bps" if spread is not None else "нет данных"
            turnover = item.get("turnover_eur")
            turnover_text = f"{number(turnover, 0)} EUR/24ч" if turnover is not None else "нет данных"
            status = "OHLC проверен" if item.get("ohlc_analyzed") else "OHLC отложен"
            lines.append(
                f"{symbol}: {item.get('signal', 'HOLD')} | спред {spread_text} | "
                f"оборот {turnover_text} | {status}"
            )
            reasons = item.get("reasons") or []
            if reasons:
                lines.append("  Причина: " + "; ".join(str(reason) for reason in reasons[:2]))
    else:
        lines.append("TONEUR/TONUSD не найдены в текущем снимке Kraken.")
    return "\n".join(lines)[:3900]



def _telegram_plain(value):
    """Convert Telegram HTML markup to safe, readable plain text."""
    import re
    text = str(value or "")
    text = re.sub(r"<a href=\"([^\"]+)\">([^<]*)</a>", lambda match: f"{match.group(2)} ({match.group(1)})", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:b|strong|i|em|u|s|code|pre)>", "", text, flags=re.IGNORECASE)
    return html.unescape(text)


def _format_report(title, body):
    """Apply the shared Telegram report header and divider layout."""
    content = str(body or "").strip()
    if content.startswith(title):
        content = content[len(title):].lstrip("\n")
    return f"{title}\n━━━━━━━━━━━━━━━━━━\n{content}\n━━━━━━━━━━━━━━━━━━"


def _format_automatic_research_report(trained):
    """Render compact, readable HTML cards for the scheduled ML research report."""
    def pct(value):
        try:
            return f"{float(value):+.2f}%"
        except (TypeError, ValueError, OverflowError):
            return "н/д"

    def auc(value):
        try:
            return f"{float(value):.3f}" if value is not None else "н/д"
        except (TypeError, ValueError, OverflowError):
            return "н/д"

    def profit_factor(value):
        if value == "INF":
            return "∞"
        try:
            number = float(value)
            return "∞" if number == float("inf") else f"{number:.2f}"
        except (TypeError, ValueError, OverflowError):
            return "н/д"

    cards = []
    for item in trained:
        passed = bool(item.get("pass"))
        status_icon = "🟢" if passed else "🔴"
        status_text = "PASSED" if passed else "NOT PASSED"
        symbol = html.escape(str(item.get("symbol") or "UNKNOWN"))
        benchmarks = item.get("benchmarks") or {}
        oos_bench = (benchmarks.get("walk_forward_oos") or {})
        holdout_bench = (benchmarks.get("independent_holdout") or {})
        oos_metrics = item.get("oos_metrics") or {}
        holdout_metrics = item.get("holdout_metrics") or {}
        buy_hold_oos = (oos_bench.get("always_long") or {}).get("net_return_pct")
        buy_hold_holdout = (holdout_bench.get("always_long") or {}).get("net_return_pct")
        momentum_oos = (oos_bench.get("positive_24h_momentum") or {}).get("net_return_pct")
        momentum_holdout = (holdout_bench.get("positive_24h_momentum") or {}).get("net_return_pct")

        lines = [
            f"{status_icon} <b>{symbol} — {status_text}</b>",
            f"📚 Выборок: <b>{int(item.get('samples') or 0):,}</b>",
            "📈 <b>WALK-FORWARD OOS</b>",
            f"Доходность: <b>{pct(item.get('oos_return'))}</b>  •  "
            f"Сделок: <b>{int(oos_metrics.get('trades') or 0)}</b>  •  "
            f"PF: <b>{profit_factor(oos_metrics.get('profit_factor'))}</b>",
            f"Просадка: <b>{pct(oos_metrics.get('max_drawdown_pct'))}</b>",
            "🧪 <b>НЕЗАВИСИМЫЙ HOLDOUT</b>",
            f"Доходность: <b>{pct(item.get('holdout_return'))}</b>  •  "
            f"Сделок: <b>{int(holdout_metrics.get('trades') or 0)}</b>",
            f"Просадка: <b>{pct(holdout_metrics.get('max_drawdown_pct'))}</b>",
            "⚖️ <b>СРАВНЕНИЕ С BENCHMARK</b>",
            f"BUY &amp; HOLD: OOS {pct(buy_hold_oos)}  •  Holdout {pct(buy_hold_holdout)}",
            f"24h Momentum: OOS {pct(momentum_oos)}  •  Holdout {pct(momentum_holdout)}",
            f"🎯 AUC: OOS <b>{auc(item.get('auc_oos'))}</b>  •  Holdout <b>{auc(item.get('auc_holdout'))}</b>",
        ]
        reasons = item.get("reasons") or []
        if reasons:
            lines.append("⛔ <b>Причины отказа</b>")
            lines.extend(f"  • {html.escape(str(reason))}" for reason in reasons[:3])
        else:
            lines.append("✅ Все исследовательские критерии пройдены.")
        lines.append("🛡 <i>Режим: SHADOW ONLY — модель не участвует в торговле.</i>")
        cards.append("\n".join(lines))

    return (
        "🧠 <b>ПЛАНОВОЕ ИССЛЕДОВАНИЕ МОДЕЛЕЙ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        + "\n\n━━━━━━━━━━━━━━━━━━\n\n".join(cards)
        + "\n\n<i>Результаты независимой проверки; автоматического допуска модели к торговле нет.</i>"
    )


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
        plain_text = _telegram_plain(text).strip()
        if plain_text and "━━━━━━━━━━━━━━━━━━" not in plain_text:
            heading, separator, remainder = plain_text.partition("\n")
            plain_text = (
                heading + "\n━━━━━━━━━━━━━━━━━━"
                + (("\n" + remainder) if separator else "")
                + "\n━━━━━━━━━━━━━━━━━━"
            )
        kwargs = {"chat_id": s.telegram_group_chat_id, "text": plain_text[:3900]}
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
                equity_text = "недоступна — нет свежего FX: " + ", ".join(missing)
            elif not quotes_fresh:
                daily_text = "недоступен — нет свежих рыночных котировок для оценки портфеля"
                equity_text = "недоступна — нет свежих котировок для всех позиций"
            else:
                try:
                    equity = engine._equity_after_liquidation(valuation_prices, fresh_rates)
                    equity_text = f"{equity:.4f} EUR"
                    daily_text = f"{engine.daily_pnl(valuation_prices):.2f} EUR (UTC)"
                except Exception as exc:
                    log.warning("PAPER status valuation unavailable: %s", type(exc).__name__)
                    daily_text = "недоступен — ошибка оценки"
                    equity_text = "недоступна — ошибка оценки"
            from .db import get_latest_paper_snapshot
            snapshot = get_latest_paper_snapshot(engine.db)
            valuation_time = (
                engine.last_scan_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                if engine.last_scan_at else "нет свежего сканирования"
            )
            if snapshot:
                snapshot_raw_time = str(snapshot["ts"]).replace("Z", "+00:00")
                try:
                    snapshot_dt = datetime.fromisoformat(snapshot_raw_time)
                    if snapshot_dt.tzinfo is None:
                        snapshot_dt = snapshot_dt.replace(tzinfo=timezone.utc)
                    snapshot_age = max(0, int((datetime.now(timezone.utc) - snapshot_dt).total_seconds()))
                    if snapshot_age < 60:
                        age_text = f"{snapshot_age} сек. назад"
                    elif snapshot_age < 3600:
                        age_text = f"{snapshot_age // 60} мин. назад"
                    else:
                        age_text = f"{snapshot_age // 3600} ч. {(snapshot_age % 3600) // 60} мин. назад"
                    snapshot_freshness = "🟢 актуальный" if snapshot_age <= max(120, 2 * int(engine.scan_interval_seconds)) else "🟡 устарел"
                except (TypeError, ValueError, OverflowError):
                    age_text = "возраст неизвестен"
                    snapshot_freshness = "🟡 проверьте время"
                snapshot_time = str(snapshot["ts"]).replace("T", " ")[:19] + " UTC"
                snapshot_text = (
                    f"{float(snapshot['equity_eur']):.4f} EUR в {snapshot_time} "
                    f"({age_text}; {snapshot_freshness})"
                )
            else:
                snapshot_text = "ещё нет сохранённых измерений"
            state_label = "🟢 АКТИВЕН" if state == "RUNNING" else "🟡 ПАУЗА"
            balance_lines = "\n".join(
                f"  <code>{html.escape(currency):5}</code> <b>{float(amount):,.6f}</b>"
                for currency, amount in sorted(engine.paper.cash_by_currency.items())
                if float(amount) > 1e-12
            ) or "  <i>Нет свободных средств</i>"
            pnl_icon = "🟢" if daily_text.startswith("+") or (daily_text[:1].isdigit()) else "🔴"
            scan = engine.last_scan_metrics
            scan_block = (
                f"  ⏱ Цикл: <b>{scan['duration_seconds']:.1f} сек.</b>\n"
                f"  🕯 Свечи: <b>{scan['ohlc_analyzed']}/{scan['ohlc_budget']}</b>\n"
                f"  🎯 Подходящих рынков: <b>{scan['eligible']}</b>\n"
                if scan else "  <i>Цикл ещё не завершён</i>\n"
            )
            return (
                "📊 <b>CRYPTO AI TRADER</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🧪 Режим: <b>PAPER</b>  •  {state_label}\n\n"
                "💼 <b>ПОРТФЕЛЬ</b>\n"
                f"  💎 Стоимость после расходов: <b>{html.escape(equity_text)}</b>\n"
                f"  📦 Открытые позиции: <b>{len(engine.paper.positions)}/{s.max_positions}</b>\n"
                f"  {pnl_icon} Результат за сегодня: <b>{html.escape(daily_text)}</b>\n\n"
                "💰 <b>СВОБОДНЫЕ СРЕДСТВА</b>\n"
                f"{balance_lines}\n\n"
                "🛡 <b>РИСК-МЕНЕДЖМЕНТ</b>\n"
                f"  Риск на сделку: <b>{s.risk_per_trade_pct:.2f}%</b>  •  Лимит сделки: <b>{s.max_trade:.2f} EUR</b>\n"
                f"  Stop-Loss: <b>{s.stop_loss_pct:.2f}%</b>  •  Take-Profit: <b>{s.take_profit_pct:.2f}%</b>\n"
                f"  Trailing Stop: <b>{getattr(s, 'trailing_stop_pct', 1.5):.2f}%</b>  •  Дневной лимит: <b>{s.max_daily_loss:.2f} EUR</b>\n\n"
                "🌐 <b>РЫНОК KRAKEN</b>\n"
                f"  Пар: <b>{len(engine.market_pairs):,}</b>  •  Котировок: <b>{engine.market_ticker_count:,}</b>\n"
                f"{scan_block}"
                f"  🔄 Анализ свечей: до <b>{s.max_candle_pairs}</b> пар за цикл\n\n"
                "🕒 <b>ОБНОВЛЕНИЕ ДАННЫХ</b>\n"
                f"  Котировки: <code>{html.escape(valuation_time)}</code>\n"
                f"  Снимок PAPER: <code>{html.escape(snapshot_text)}</code>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "<i>Оценка и P/L доступны только при свежих котировках и FX-курсах всех активов.</i>"
            )
        message = await context.application.bot_data["run_blocking"].run(read_status)
        await update.effective_message.reply_text(message, parse_mode="HTML")

    async def monitorstatus(update, context):
        if not await authorized(update): return await deny(update)
        def read_monitor_status():
            from .db import get_state, get_backup_health
            from .monitoring import format_monitor_status
            state = get_state(monitor_db, "external_monitor_state", {})
            if not isinstance(state, dict):
                state = {}
            report = format_monitor_status(s, state)
            backup = get_backup_health(Path(s.db_path).expanduser().parent / "backups", max_age_hours=26, keep=7)
            if backup["count"] == 0:
                backup_block = (
                    "🗄 SQLITE BACKUP\n"
                    "🔴 РЕЗЕРВНАЯ КОПИЯ НЕ НАЙДЕНА\n"
                    "Проверка: нет данных  •  Копий: 0/7"
                )
            else:
                age_hours = backup["age_seconds"] / 3600.0
                healthy = bool(backup["healthy"])
                backup_block = (
                    "🗄 SQLITE BACKUP\n"
                    f"{'🟢 РЕЗЕРВНАЯ КОПИЯ В ПОРЯДКЕ' if healthy else '🔴 ТРЕБУЕТСЯ ПРОВЕРКА'}\n"
                    f"🕒 Дата: {backup['latest_at']} UTC\n"
                    f"⏱ Возраст: {age_hours:.1f} ч  •  "
                    f"Integrity: {backup['integrity']}  •  "
                    f"Копий: {backup['count']}/7"
                )
            return report + "\n━━━━━━━━━━━━━━━━━━\n" + backup_block
        message = await context.application.bot_data["monitor_runner"].run(read_monitor_status)
        await update.effective_message.reply_text(message)

    async def dbhealth(update, context):
        if not await authorized(update): return await deny(update)

        def inspect_database():
            from .db import get_backup_health
            current = engine.db.execute("PRAGMA quick_check").fetchone()
            integrity = str(current[0]) if current else "no_result"
            db_path = Path(s.db_path).expanduser()
            backup = get_backup_health(db_path.parent / "backups", max_age_hours=26, keep=7)
            db_size = db_path.stat().st_size if db_path.exists() else 0
            wal_path = Path(str(db_path) + "-wal")
            wal_size = wal_path.stat().st_size if wal_path.exists() else 0
            return integrity, db_size, wal_size, backup

        try:
            integrity, db_size, wal_size, backup = await context.application.bot_data["run_blocking"].run(inspect_database)
            age = backup["age_seconds"]
            age_text = "нет резервной копии" if age is None else f"{int(age // 3600)} ч {int((age % 3600) // 60)} мин"
            status_text = "OK" if integrity == "ok" and backup["healthy"] else "ТРЕБУЕТ ВНИМАНИЯ"
            message = (
                "🗄 ДИАГНОСТИКА SQLITE\n━━━━━━━━━━━━━━━━━━\n"
                f"🛡 Общий статус: {status_text}\n\n💾 РАБОЧАЯ БАЗА ДАННЫХ\n"
                f"Проверка рабочей БД: {integrity}\n"
                f"  Размер БД: {db_size / 1024 / 1024:.2f} MB  •  WAL: {wal_size / 1024 / 1024:.2f} MB\n\n📦 РЕЗЕРВНЫЕ КОПИИ\n"
                f"  Количество: {backup['count']}/7\n"
                f"  Возраст последней: {age_text}\n"
                f"  Integrity: {backup['integrity']}\n"
                f"  Лимит возраста: 26 ч  •  Ротация: {'OK' if backup['retention_ok'] else 'ПРЕВЫШЕНА'}\n━━━━━━━━━━━━━━━━━━"
            )
            await update.effective_message.reply_text(message)
        except Exception as exc:
            log.error("Database health check failed: %s", type(exc).__name__)
            await update.effective_message.reply_text(f"🚨 Проверка SQLite не выполнена: {type(exc).__name__}")

    async def scan(update, context):
        if not await authorized(update): return await deny(update)
        await update.effective_message.reply_text("🔎 Сканирование рынков Kraken…")
        try:
            def run_scan():
                rows = engine.scan()
                return rows, len(engine.market_pairs), engine.market_ticker_count
            rows, pair_count, ticker_count = await context.application.bot_data["run_blocking"].run(run_scan)
            message = _format_scan_report(rows, pair_count, ticker_count)
            await update.effective_message.reply_text(message)
        except Exception as exc:
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
                await update.effective_message.reply_text(_format_report(lines[0], "\n".join(lines))[:3900])
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
"━━━━━━━━━━━━━━━━━━\nЭто тест одного рынка, не прогноз и не гарантия будущей прибыли."
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
            return _format_report("💰 СВОБОДНЫЕ СРЕДСТВА PAPER", "Виртуальные остатки:\n" + ("\n".join(balances) or "Нулевые остатки.") + "\n\nРеальные средства не затрагиваются.")
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
            runner = context.application.bot_data["run_blocking"]
            balances = await runner.run(exchange.balance)

            # Value real assets only against recent, fresh EUR market marks.
            # Old or unavailable rates are shown as unknown, never as zero.
            rates, scanned_at = await runner.run(
                lambda: (engine._fresh_fx_rates(engine.last_prices), engine.last_scan_at)
            )
            from datetime import datetime, timezone
            if scanned_at is None:
                rates = {"EUR": 1.0}
            else:
                age = (datetime.now(timezone.utc) - scanned_at).total_seconds()
                if age < 0 or age > max(120, 2 * int(engine.scan_interval_seconds)):
                    rates = {"EUR": 1.0}

            from .wallet import build_wallet_inventory
            inventory = build_wallet_inventory(balances, rates)
            lines = []
            for item in inventory["assets"]:
                amount_text = f'{item["amount"]:.12g}'
                if item["value_eur"] is None:
                    value_text = "оценка EUR недоступна"
                else:
                    value_text = f'{item["value_eur"]:.2f} EUR'
                lines.append(f'{item["asset"]}: {amount_text} — {value_text}')

            if inventory["asset_count"] == 0:
                body = "Положительных остатков не найдено. Баланс: 0 активов / 0.00 EUR."
            else:
                body = "\n".join(lines)
                if inventory["total_complete"]:
                    body += f'\n\nИТОГО: {inventory["total_eur"]:.2f} EUR'
                else:
                    body += (
                        "\n\nПолная оценка в EUR пока невозможна. "
                        "Нет свежего прямого EUR-курса для: "
                        + ", ".join(inventory["unknown_assets"])
                        + f'. Известная часть: {inventory["known_total_eur"]:.2f} EUR.'
                    )

            await update.effective_message.reply_text(
                "🏦 РЕАЛЬНЫЙ БАЛАНС KRAKEN\n"
                + body
                + "\n\nТолько чтение. Реальные средства не импортируются в виртуальный PAPER-кошелёк. "
                "Реальные ордера отключены.\\n━━━━━━━━━━━━━━━━━━"
            )
        except Exception as exc:
            log.error("Read-only Kraken balance request failed: %s", type(exc).__name__)
            await update.effective_message.reply_text(format_error(exc, "/realbalance", update.effective_user))

    async def walletcheck(update, context):
        if not await authorized(update): return await deny(update)
        if not s.kraken_api_key or not s.kraken_api_secret:
            return await update.effective_message.reply_text(
                "Проверка Kraken недоступна: не заданы API-ключи. Нужны только права Query Funds."
            )
        await update.effective_message.reply_text("Проверяю остатки и торговые ограничения Kraken (только чтение)…")
        try:
            runner = context.application.bot_data["run_blocking"]
            balances, pairs, market_state = await runner.run(
                lambda: (exchange.balance(), exchange.discover_pairs(), (
                    engine._fresh_fx_rates(engine.last_prices), engine.last_scan_at
                ))
            )
            rates, scanned_at = market_state
            from datetime import datetime, timezone
            if scanned_at is None:
                rates = {"EUR": 1.0}
            else:
                age = (datetime.now(timezone.utc) - scanned_at).total_seconds()
                if age < 0 or age > max(120, 2 * int(engine.scan_interval_seconds)):
                    rates = {"EUR": 1.0}
            from .wallet import build_wallet_inventory, build_asset_pair_routes
            known_assets = {
                str(pair.get(field) or "").strip().upper()
                for pair in pairs
                for field in ("base", "quote")
                if pair.get(field)
            }
            inventory = build_wallet_inventory(balances, rates, known_assets=known_assets)
            routes = build_asset_pair_routes(
                [item["asset"] for item in inventory["assets"]], pairs
            )
            lines = ["🏦 ПРЕДТОРГОВАЯ ПРОВЕРКА KRAKEN", "━━━━━━━━━━━━━━━━━━", "💼 СОСТОЯНИЕ КОШЕЛЬКА",
                     f"Активов с положительным остатком: {inventory['asset_count']}"]
            if inventory["asset_count"] == 0:
                lines.append("Кошелёк пуст. Проверка торговых маршрутов будет доступна после пополнения.")
            else:
                for item in inventory["assets"]:
                    asset = item["asset"]
                    value = (f'{item["value_eur"]:.2f} EUR'
                             if item["value_eur"] is not None else "оценка EUR недоступна")
                    lines.append(f"\n{asset}: {item['amount']:.12g} — {value}")
                    asset_routes = routes.get(item["canonical_asset"], [])
                    if not asset_routes:
                        lines.append("  Торговые пары: не найдены среди онлайн spot-рынков")
                        continue
                    lines.append(f"  Доступных spot-пар: {len(asset_routes)}")
                    for route in asset_routes[:12]:
                        limits = []
                        if route["ordermin"] not in (None, ""):
                            limits.append(f'min qty {route["ordermin"]}')
                        if route["costmin"] not in (None, ""):
                            limits.append(f'min cost {route["costmin"]}')
                        fee_rows = route["fees"] or []
                        if isinstance(fee_rows, list) and fee_rows and isinstance(fee_rows[0], (list, tuple)) and len(fee_rows[0]) > 1:
                            try:
                                limits.append(f'fee {float(fee_rows[0][1]):g}%')
                            except (TypeError, ValueError):
                                pass
                        suffix = "; ".join(limits) if limits else "ограничения не опубликованы в ответе API"
                        lines.append(f'  • {route["symbol"]}: {suffix}')
                    if len(asset_routes) > 12:
                        lines.append(f"  …ещё {len(asset_routes) - 12} пар")
            lines.append("\nПроверки риска PAPER: лимит сделки "
                         f'{s.max_trade:.2f} EUR; дневной убыток не более {s.max_daily_loss:.2f} EUR; '
                         f'максимум позиций {s.max_positions}; спред до {s.max_spread_bps:.1f} bps.')
            lines.append("Минимумы и комиссии берутся из метаданных Kraken; отсутствующие значения не угадываются.")
            lines.append("Только чтение: реальные ордера, переводы и импорт средств в PAPER отключены.")
            lines.append("━━━━━━━━━━━━━━━━━━")
            await update.effective_message.reply_text("\n".join(lines)[:3900])
        except Exception as exc:
            log.error("Read-only Kraken wallet preflight failed: %s", type(exc).__name__)
            await update.effective_message.reply_text(format_error(exc, "/walletcheck", update.effective_user))

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
                "━━━━━━━━━━━━━━━━━━\n"
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
                "Модель остаётся SHADOW ONLY: автоматического внедрения в торговлю нет.\n"
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
            lines = ["🧠 ИССЛЕДОВАТЕЛЬСКИЕ МОДЕЛИ", "━━━━━━━━━━━━━━━━━━", "Статус: SHADOW ONLY — модели не торгуют"]
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
        if len(lines) == 3:
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
            lines = ["🧠 ИСТОРИЯ ИССЛЕДОВАНИЙ — SHADOW ONLY", "━━━━━━━━━━━━━━━━━━", "Последние 10 запусков; результаты не торгуют автоматически."]
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
                if not metrics["closed_trades"]:
                    return await update.effective_message.reply_text(
                        f"За последние {hours} ч. нет сохранённых PAPER-измерений или закрытых сделок. "
                        "Запусти /run и оставь PAPER работать для сбора статистики."
                    )
                return await update.effective_message.reply_text(
                    f"📊 PAPER СДЕЛКИ — {hours} ч.\n"
                    f"Закрытых сделок: {metrics['closed_trades']}\n"
                    f"Прибыльных: {metrics['winning_trades']} | убыточных: {metrics['losing_trades']}\n"
                    f"Win rate: {metrics['win_rate_pct']:.1f}% | Profit factor: {metrics['profit_factor']}\n"
                    f"Реализованный P/L: {metrics['realized_pnl_eur']:+.4f} EUR\n"
                    f"Комиссии: {metrics['total_fees_eur']:.4f} EUR"
                )
            by_symbol_text = ", ".join(
                "{} {:+.4f} EUR".format(item["symbol"], item["realized_pnl_eur"])
                for item in metrics["by_symbol"][:5]
            ) or "пока нет закрытых сделок"
            span_seconds = int(metrics.get("measurement_span_seconds", 0))
            span_hours, remainder_seconds = divmod(span_seconds, 3600)
            span_minutes = remainder_seconds // 60
            span_text = (
                f"{span_hours} ч {span_minutes} мин"
                if span_hours else f"{span_minutes} мин"
            )
            coverage_pct = min(100.0, span_seconds / (hours * 3600) * 100.0)
            await update.effective_message.reply_text(
                f"📈 PAPER FORWARD-TEST — окно {hours} ч.\n"
                f"Измерений: {metrics['samples']}\n"
                f"Размах измерений: {span_text} ({coverage_pct:.1f}% окна; не гарантия непрерывной работы)\n"
                f"Капитал: {metrics['start_equity_eur']:.4f} → {metrics['end_equity_eur']:.4f} EUR\n"
                f"Изменение: {metrics['change_eur']:+.4f} EUR ({metrics['change_pct']:+.2f}%)\n"
                f"Макс. наблюдаемая просадка: {metrics['max_observed_drawdown_pct']:.2f}%\n"
                f"Среднее число позиций: {metrics['avg_open_positions']:.2f}\n"
                f"Сигналов BUY: {metrics['buy_signals']}\n"
                f"Среднее число анализируемых рынков за цикл: {metrics['avg_analyzed_markets']:.1f}\n"
                f"Закрытых сделок: {metrics['closed_trades']} | без EUR-оценки: {metrics['closed_trades_without_eur_valuation']}\n"
                f"Прибыльных: {metrics['winning_trades']} | убыточных: {metrics['losing_trades']}\n"
                f"Win rate: {metrics['win_rate_pct']:.1f}% | Profit factor: {metrics['profit_factor']}\n"
                f"Реализованный P/L: {metrics['realized_pnl_eur']:+.4f} EUR\n"
                f"Комиссии по исполнениям: {metrics['total_fees_eur']:.4f} EUR | без EUR-оценки: {metrics['fills_without_eur_fee_valuation']}\n"
                f"По монетам: {by_symbol_text}\n"
                f"Период: {metrics['first_ts']} — {metrics['last_ts']} UTC\n"
                "━━━━━━━━━━━━━━━━━━\nЭто изменение стоимости PAPER-портфеля, а не доказательство будущей доходности."
            )
        except Exception as exc:
            await update.effective_message.reply_text(f"Ошибка отчёта: {safe_error_message(exc)}")

    async def interval(update, context):
        if not await authorized(update): return await deny(update)
        allowed = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "24h": 86400}
        if not context.args or context.args[0] not in allowed:
            return await update.effective_message.reply_text("⏱ ИНТЕРВАЛ СКАНИРОВАНИЯ\n━━━━━━━━━━━━━━━━━━\nИспользование: /interval 1m|5m|15m|30m|1h|4h|24h\nСканер Kraken проверяет рынок по заданному интервалу.\n━━━━━━━━━━━━━━━━━━")
        await context.application.bot_data["run_blocking"].run(
            lambda: setattr(engine, "scan_interval_seconds", allowed[context.args[0]])
        )
        await update.effective_message.reply_text(f"⏱ ИНТЕРВАЛ СКАНИРОВАНИЯ\n━━━━━━━━━━━━━━━━━━\nУстановлен: {context.args[0]}\nИзменение действует до перезапуска сервиса.\n━━━━━━━━━━━━━━━━━━")

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
            return _format_report("💼 ПОРТФЕЛЬ PAPER", "💰 СВОБОДНЫЕ СРЕДСТВА\n" + ("\n".join(balances) or "Нет свободных средств.") + "\n\n🛡 ОЦЕНКА И FX\n" + fx_text + "\n\n📦 ОТКРЫТЫЕ ПОЗИЦИИ\n" + ("\n".join(lines) or "Нет открытых позиций."))
        message = await context.application.bot_data["run_blocking"].run(read_portfolio)
        await update.effective_message.reply_text(message)

    async def run(update, context):
        if not await authorized(update): return await deny(update)
        await context.application.bot_data["run_blocking"].run(engine.start)
        await update.effective_message.reply_text("▶️ PAPER — ЗАПУСК\n━━━━━━━━━━━━━━━━━━\nСтатус: активен\nАвтосканирование включено.\n━━━━━━━━━━━━━━━━━━")

    async def pause(update, context):
        if not await authorized(update): return await deny(update)
        await context.application.bot_data["run_blocking"].run(lambda: engine.stop("Paused by owner"))
        await update.effective_message.reply_text("⏸ PAPER — ПАУЗА\n━━━━━━━━━━━━━━━━━━\nНовые сделки остановлены.\nОткрытые позиции сохраняются; мониторинг рынка продолжается.\n━━━━━━━━━━━━━━━━━━")

    async def stop(update, context):
        if not await authorized(update): return await deny(update)
        await context.application.bot_data["run_blocking"].run(lambda: engine.stop("Emergency stop via Telegram"))
        await update.effective_message.reply_text("⛔ PAPER — STOP\n━━━━━━━━━━━━━━━━━━\nАвтоматические сделки остановлены.\nОткрытые позиции не удалены.\n━━━━━━━━━━━━━━━━━━")

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
            "/start — меню\n/status — состояние и P/L\n/dbhealth — целостность SQLite и свежесть резервной копии\n/monitorstatus — диагностика Render/GitHub/Telegram\n/scan — сканирование\n"
            "/account — виртуальные остатки PAPER\n/realbalance — реальные остатки Kraken (только чтение)\n/walletcheck — пары, минимумы и комиссии Kraken (только чтение)\n"
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
                previous = get_state(research_db, "ml_auto_research_date", {})
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
                by_symbol = {item["symbol"]: item for item in tickers}
                prices = {
                    item["symbol"]: float(item.get("lastPrice") or 0)
                    for item in tickers if float(item.get("lastPrice") or 0) > 0
                }
                from .engine import fresh_fx_rates_from_market
                rates = fresh_fx_rates_from_market(s, pairs, by_symbol, prices)
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
                cursor = get_state(research_db, "ml_auto_research_cursor", 0)
                selected, next_cursor = rotate_research_batch(
                    eligible, cursor, s.ml_auto_research_markets
                )
                if selected:
                    set_state(research_db, "ml_auto_research_cursor", next_cursor)
                log.info("Automatic ML research selected %d markets for this batch", len(selected))
                trained = []
                for _, symbol in selected:
                    try:
                        candidate = perform_research(
                            research_db, exchange, s, symbol, source="automatic"
                        )
                        trained.append({
                            "symbol": symbol,
                            "pass": bool(candidate["research_pass"]),
                            "samples": candidate["samples"],
                            "version": candidate["model_version"],
                            "reasons": candidate.get("rejection_reasons", []),
                            "oos_return": candidate["out_of_sample"]["net_return_pct"],
                            "holdout_return": candidate["independent_holdout"]["net_return_pct"],
                            "oos_metrics": candidate.get("out_of_sample", {}),
                            "holdout_metrics": candidate.get("independent_holdout", {}),
                            "benchmarks": candidate.get("benchmark_metrics", {}),
                            "auc_oos": candidate.get("score_ranking", {}).get("walk_forward_oos_roc_auc"),
                            "auc_holdout": candidate.get("score_ranking", {}).get("independent_holdout_roc_auc"),
                        })
                    except Exception as market_exc:
                        log.warning("Automatic model research skipped %s: %s", symbol, market_exc)
                log.info("Automatic ML research completed: %d of %d selected markets trained",
                         len(trained), len(selected))
                if trained:
                    set_state(research_db, "ml_auto_research_date", {
                        "at_utc": now_utc.isoformat(),
                        "markets": trained,
                        "cadence_hours": 1,
                    })
                return trained

            trained = await context.application.bot_data["research_runner"].run(research_top_markets)
            if trained:
                report = _format_automatic_research_report(trained)
                await send_topic(
                    context, s.topic_general_id, report, parse_mode="HTML"
                )
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
                    "📊 CRYPTO AI TRADER — АВТОМАТИЧЕСКИЙ ОТЧЁТ\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"🕒 Последнее успешное сканирование: {scan_time}\n\n"
                    "💼 ПОРТФЕЛЬ PAPER\n"
                    f"  💰 Свободные средства: {state['cash']}\n"
                    f"  📦 Открытые позиции: {len(state['positions'])}/{s.max_positions}\n"
                    f"  📈 Результат за сегодня: {daily_text}\n\n"
                    "🔎 РЫНОЧНЫЕ СИГНАЛЫ\n" + ("\n".join(signal_lines) or "Нет данных") +
                    "\n\n📦 ОТКРЫТЫЕ ПОЗИЦИИ\n" + ("\n".join(position_lines) or "Нет открытых позиций") +
                    "\n━━━━━━━━━━━━━━━━━━"
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

    # External event alerts must tolerate transient Telegram API latency.
    telegram_request = HTTPXRequest(
        connect_timeout=10, read_timeout=30, write_timeout=30, pool_timeout=10,
        connection_pool_size=16,
    )
    async def close_auxiliary_dbs(application):
        import asyncio
        if monitor_db is not engine.db:
            await asyncio.to_thread(monitor_db.close)
        if research_db is not engine.db and research_db is not monitor_db:
            await asyncio.to_thread(research_db.close)

    app = (
        Application.builder()
        .token(s.bot_token)
        .request(telegram_request)
        .post_shutdown(close_auxiliary_dbs)
        .build()
    )
    # Keep exchange/engine work serialized on its own connection. External
    # monitor state uses a second SQLite connection and runner so network/API
    # monitoring never queues behind a full market scan.
    from .worker import SerializedRunner
    from .db import connect
    engine_runner = SerializedRunner()
    monitor_db = engine.db if s.db_path == ":memory:" else connect(s.db_path)
    research_db = engine.db if s.db_path == ":memory:" else connect(s.db_path)
    monitor_runner = engine_runner if monitor_db is engine.db else SerializedRunner()
    research_runner = engine_runner if research_db is engine.db else SerializedRunner()
    app.bot_data["run_blocking"] = engine_runner
    app.bot_data["monitor_db"] = monitor_db
    app.bot_data["monitor_runner"] = monitor_runner
    app.bot_data["research_runner"] = research_runner
    for name, fn in [("start", start), ("status", status), ("dbhealth", dbhealth), ("monitorstatus", monitorstatus), ("scan", scan), ("account", account), ("realbalance", realbalance), ("walletcheck", walletcheck), ("paperreport", paperreport), ("learn", learn), ("learning", learning), ("research", research_history_cmd), ("backtest", backtest), ("interval", interval), ("portfolio", portfolio), ("topicid", topicid), ("testtopics", testtopics),
                     ("run", run), ("pause", pause), ("stop", stop), ("help", help_cmd)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CommandHandler("test_topics", testtopics))  # Backwards-compatible alias shown in /help.
    app.add_handler(CallbackQueryHandler(callback))

    async def external_monitor_job(context):
        """Poll optional Render/GitHub APIs and route status changes to Telegram."""
        if not (s.render_api_key and s.render_service_id or s.github_repository):
            return
        stage = "polling external APIs"
        state = None
        try:
            import asyncio
            from .monitoring import poll_external_events, merge_monitor_health_state

            # Read/write the shared SQLite connection under the engine lock,
            # but never hold that lock while waiting on Render/GitHub HTTP APIs.
            def load_monitor_state():
                from .db import get_state
                value = get_state(monitor_db, "external_monitor_state", {})
                return value if isinstance(value, dict) else {}

            # Do not advance durable event cursors until Telegram has accepted
            # every alert. A delivery failure therefore retries the same events.
            if not bool(s.telegram_group_chat_id):
                log.warning("External monitoring has no Telegram destination; keeping event cursors unchanged")
                return

            state = await context.application.bot_data["monitor_runner"].run(load_monitor_state)
            events = await asyncio.to_thread(poll_external_events, s, state)

            # Save API health immediately, independently of Telegram delivery,
            # while leaving event cursors untouched for retry safety.
            stage = "persisting API health"
            def persist_monitor_health():
                from .db import get_state, set_state
                persisted = get_state(monitor_db, "external_monitor_state", {})
                set_state(
                    monitor_db,
                    "external_monitor_state",
                    merge_monitor_health_state(persisted, state),
                )
            await context.application.bot_data["monitor_runner"].run(persist_monitor_health)
            log.info("External monitoring APIs polled; pending_notifications=%d", len(events))
            for event in events:
                stage = f"sending {event.get('source', 'external')} notification"
                source = html.escape(event["source"])
                status = html.escape(event["status"])
                title = html.escape(event["title"])
                detail = html.escape(event["detail"][:700])
                url = event.get("url", "")
                link = f'\n<a href="{html.escape(url, quote=True)}">Открыть событие</a>' if url.startswith("https://") else ""
                successful = event["status"] in {"LIVE", "SUCCESS", "RECOVERED"}
                heading = "🟢 СИСТЕМНОЕ ИЗВЕЩЕНИЕ" if successful else "🚨 СБОЙ ВНЕШНЕГО СЕРВИСА"
                message = (
                    f"<b>{heading}</b>\n"
                    f"🕒 Время: <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</code>\n"
                    f"🧩 Источник: <b>{source}</b>\n"
                    f"📌 Событие: <b>{title}</b>\n"
                    f"⚠️ Статус: <code>{status}</code>\n"
                    f"📝 Описание: {detail}{link}"
                )
                topic = s.topic_general_id if successful else s.topic_errors_id
                await send_topic(context, topic, message, parse_mode="HTML")

            # Commit only after all sends succeeded. Empty event batches still
            # commit polling cursors so the initial baseline is persisted.
            stage = "persisting monitoring state"
            state["last_poll_at"] = datetime.now(timezone.utc).isoformat()
            state["last_events_count"] = len(events)
            if events:
                state["last_alert_delivery_at"] = state["last_poll_at"]
            def persist_monitor_state():
                from .db import set_state
                set_state(monitor_db, "external_monitor_state", state)
            await context.application.bot_data["monitor_runner"].run(persist_monitor_state)
            log.info("External monitoring cycle succeeded: new_events=%d", len(events))
        except Exception as exc:
            log.warning("External monitoring job failed during %s: %s", stage, type(exc).__name__)


    async def database_backup_job(context):
        """Back up the persistent SQLite database without blocking the bot loop."""
        runner = context.application.bot_data["run_blocking"]
        backup_dir = Path(s.db_path).expanduser().parent / "backups"
        try:
            backup_path = await runner.run(
                lambda: backup_database(engine.db, backup_dir, keep=7)
            )
            log.info("SQLite backup completed and verified: %s", backup_path)
        except Exception as exc:
            log.error("SQLite backup failed: %s", type(exc).__name__)
            try:
                await send_topic(
                    context, s.topic_errors_id,
                    f"🚨 Резервное копирование SQLite не выполнено. Тип ошибки: {type(exc).__name__}",
                )
            except Exception:
                log.error("Could not send SQLite backup failure notification")

    async def backup_health_job(context):
        """Alert once on SQLite backup health transitions and confirm recovery."""
        from .db import get_backup_health, get_state, set_state
        from .monitoring import backup_health_transition

        backup_dir = Path(s.db_path).expanduser().parent / "backups"
        try:
            current = await context.application.bot_data["run_blocking"].run(
                lambda: get_backup_health(backup_dir, max_age_hours=26, keep=7)
            )
            if current["count"] == 0:
                new_status = "missing"
            elif current["integrity"] != "ok":
                new_status = "corrupt"
            elif not current["retention_ok"]:
                new_status = "retention"
            elif not current["healthy"]:
                new_status = "stale"
            else:
                new_status = "fresh"

            def read_previous():
                value = get_state(monitor_db, "sqlite_backup_health", {})
                return value if isinstance(value, dict) else {}
            previous = await context.application.bot_data["monitor_runner"].run(read_previous)
            transition = backup_health_transition(previous.get("status"), new_status)

            if transition == "failure":
                if new_status == "missing":
                    detail = "Резервная копия SQLite не найдена."
                elif new_status == "corrupt":
                    detail = f"Integrity последней копии не пройден: {current['integrity']}."
                elif new_status == "retention":
                    detail = f"Количество резервных копий превышает лимит 7: {current['count']}."
                else:
                    detail = f"Последняя резервная копия устарела: {current['age_seconds'] / 3600:.1f} ч."
                await send_topic(
                    context, s.topic_errors_id,
                    "🚨 <b>ПРОБЛЕМА РЕЗЕРВНОГО КОПИРОВАНИЯ</b>\n"
                    f"🕒 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                    f"⚠️ {html.escape(detail)}",
                    parse_mode="HTML",
                )
            elif transition == "recovered":
                await send_topic(
                    context, s.topic_general_id,
                    "🟢 <b>РЕЗЕРВНОЕ КОПИРОВАНИЕ ВОССТАНОВЛЕНО</b>\n"
                    f"Последняя проверенная копия: {current['latest_at']}",
                    parse_mode="HTML",
                )

            def persist_health():
                set_state(monitor_db, "sqlite_backup_health", {
                    "status": new_status,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "latest_at": current.get("latest_at"),
                    "age_seconds": current.get("age_seconds"),
                    "integrity": current.get("integrity"),
                    "count": current.get("count", 0),
                })
            await context.application.bot_data["monitor_runner"].run(persist_health)
            log.info("SQLite backup health check: %s", new_status)
        except Exception as exc:
            log.error("SQLite backup health check failed: %s", type(exc).__name__)

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
    # Optional API polling is enabled only when both credentials and resource IDs are configured.
    app.job_queue.run_repeating(external_monitor_job, interval=90, first=25, name="external-platform-monitor")
    # Daily verified SQLite backup on the persistent Render disk; retain seven copies.
    app.job_queue.run_repeating(database_backup_job, interval=86400, first=300, name="sqlite-daily-backup")
    app.job_queue.run_repeating(backup_health_job, interval=3600, first=600, name="sqlite-backup-health")
    # Start once, then cycle_job schedules the next scan after completion.
    # This fixed-delay cadence avoids overlapping jobs and skipped scans.
    app.job_queue.run_once(cycle_job, when=10, name="paper-cycle")
    app.job_queue.run_repeating(ml_research_job, interval=3600, first=60, name="hourly-ml-research")
    return app
