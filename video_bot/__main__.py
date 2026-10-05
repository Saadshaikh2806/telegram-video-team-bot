import argparse
import logging
import os
import signal
import threading
from pathlib import Path

from .config import Config
from .engine import Engine
from .runner import Runner
from .store import Store
from .telegram import Telegram, TelegramError
from .health import Health
from .keepalive import start_keepalive
from .database_errors import database_error_hint


def main():
    parser = argparse.ArgumentParser(description='Telegram video assignment bot')
    parser.add_argument('--check', action='store_true', help='Validate local configuration without contacting Telegram')
    args = parser.parse_args()
    try:
        config = Config.from_env()
    except (ValueError, KeyError) as exc:
        raise SystemExit(f'Configuration needs attention: {exc}') from None
    if not config.token:
        raise SystemExit('Add TELEGRAM_BOT_TOKEN to .env first. Run python setup_bot.py for guided setup.')
    if args.check:
        print('Configuration is valid.' + (' Add ADMIN_IDS before using admin commands.' if not config.admins else ''))
        return
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    api = Telegram(config.token)
    try:
        api.call('getMe')
        if api.call('getWebhookInfo').get('url'):
            raise SystemExit('This bot has a webhook. Use a separate bot or remove its webhook before starting this polling bot.')
    except TelegramError as exc:
        raise SystemExit(f'Telegram connection failed (code {exc.code}); check token and group access.') from None
    try:
        store = Store(config.database, config.database_url)
    except Exception as exc:
        raise SystemExit('Could not open the database. ' + database_error_hint(exc, config.database_url) + ' Credentials have not been logged.') from None
    health = Health()
    if os.getenv('PORT'):
        health.start(int(os.environ['PORT']))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    if health.server:
        start_keepalive(stop)
    print('Bot is running. In Telegram, send /whoami or /help. Press Ctrl+C to stop.')
    if not config.admins:
        print('ADMIN_IDS is empty. Use /whoami, add your ID to .env, then restart.')
    try:
        Runner(Engine(config, store), api).run(stop, health)
    except KeyboardInterrupt:
        print('\nBot stopped. Jobs and deadlines are saved.')
    finally:
        stop.set()
        try:
            store.release_lease()
        finally:
            store.db.close()
            if health.server:
                health.server.shutdown()


if __name__ == '__main__':
    main()
