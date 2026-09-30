"""Best-effort self-ping, matching Kutty bot's hosting approach.

This only runs while the process is alive. An external monitor is still useful
for waking an already sleeping service; platform uptime is not guaranteed.
"""
import logging
import os
import threading
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


log = logging.getLogger(__name__)
INTERVAL_SECONDS = 5 * 60


def health_url(base):
    parsed = urlsplit(base.strip())
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Keep-alive needs the public HTTPS service URL without credentials')
    if parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Keep-alive needs the service origin, without a path, query, or fragment')
    return base.strip().rstrip('/') + '/health'


def ping(url):
    request = Request(url, headers={'User-Agent': 'VideoTeamBot-Health/1.0'})
    try:
        with urlopen(request, timeout=10) as response:
            status = response.status
            response.read(256)
        log.info('Keep-alive health request returned %s', status)
        return status == 200
    except Exception:
        # Avoid logging URLs or request details, which may contain configuration.
        log.warning('Keep-alive request failed; will retry at the next interval')
        return False


def start_keepalive(stop):
    if os.getenv('KEEP_ALIVE_ENABLED', 'true').lower() != 'true':
        return None
    base = os.getenv('RENDER_EXTERNAL_URL') or os.getenv('APP_URL', '')
    if not base:
        return None
    try:
        url = health_url(base)
    except ValueError:
        log.warning('Keep-alive disabled: use a public HTTPS service origin')
        return None

    def run():
        if stop.wait(5):
            return
        while not stop.is_set():
            ping(url)
            if stop.wait(INTERVAL_SECONDS):
                break

    worker = threading.Thread(target=run, name='keep-alive', daemon=True)
    worker.start()
    log.info('Best-effort self-ping enabled: every 5 minutes')
    return worker
