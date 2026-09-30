"""Small Bot API client. Never log URLs: they contain the bot token."""
import json
import mimetypes
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(Exception):
    def __init__(self, code, retry_after=0):
        self.code, self.retry_after = code, retry_after
        super().__init__(f'Telegram request failed (code {code})')


class Telegram:
    def __init__(self, token):
        self.base = f'https://api.telegram.org/bot{token}/'

    def call(self, method, **payload):
        file_path = payload.pop('_file', None)
        if file_path:
            path = Path(file_path)
            boundary = uuid.uuid4().hex
            parts = []
            for key, value in payload.items():
                value = json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
            field = 'photo' if method == 'sendPhoto' else 'document'
            mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
            parts.extend([path.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()])
            body = b''.join(parts)
            content_type = f'multipart/form-data; boundary={boundary}'
        else:
            body = json.dumps(payload).encode()
            content_type = 'application/json'
        try:
            req = Request(self.base + method, data=body, headers={'Content-Type': content_type})
            with urlopen(req, timeout=40) as response:
                result = json.load(response)
        except HTTPError as exc:
            try:
                result = json.loads(exc.read())
            except (ValueError, OSError):
                raise TelegramError(exc.code) from None
        except (URLError, TimeoutError, OSError):
            raise TelegramError(0) from None
        if not result.get('ok'):
            raise TelegramError(result.get('error_code', 0), result.get('parameters', {}).get('retry_after', 0))
        return result['result']
