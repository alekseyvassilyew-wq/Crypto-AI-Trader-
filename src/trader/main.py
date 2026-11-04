import logging
from .config import load_settings
from .db import connect, audit
from .exchange import KrakenSpot
from .paper import PaperBroker
from .engine import Engine
from .telegram_ui import build_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# Telegram's HTTP client logs request URLs at INFO; Bot API URLs contain the
# bot token. Never allow httpx/httpcore to write those URLs to persistent logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

def main():
    settings = load_settings()
    db = connect(settings.db_path)
    audit(db, "STARTUP", "exchange=Kraken; mode=PAPER")
    exchange = KrakenSpot(settings.kraken_api_key, settings.kraken_api_secret)
    # Public market data is available without credentials. If credentials are supplied,
    # verify Query Funds permission at startup; never print balances or secrets to logs.
    if settings.kraken_api_key and settings.kraken_api_secret:
        balances = exchange.balance()
        logging.info("Kraken private API check passed; %d balance asset entries returned.", len(balances))
    else:
        logging.warning("Kraken API credentials not set; public market data only. Add credentials to enable balance check.")
    broker = PaperBroker(settings.paper_start, settings.fee_rate, db=db)
    engine = Engine(settings, db, exchange, broker)
    logging.info("Starting PAPER-only service. Real exchange order placement is not implemented.")
    build_app(settings, engine, exchange).run_polling(drop_pending_updates=True)

if __name__ == "__main__": main()
