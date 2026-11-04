def test_telegram_ui_module_imports_without_starting_the_bot():
    from src.trader import telegram_ui

    assert callable(telegram_ui.build_app)
