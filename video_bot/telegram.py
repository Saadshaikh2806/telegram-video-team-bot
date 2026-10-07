"""Small Bot API client. Never log URLs: they contain the bot token."""
import json
import mimetypes
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(Exception):
    def __init__(self, code, retry_after=0, description='', migrate_to_chat_id=None):
        self.code, self.retry_after = code, retry_after
        self.migrate_to_chat_id = migrate_to_chat_id
        self.message_not_modified = code == 400 and 'message is not modified' in description.lower()
        self.message_to_edit_missing = code == 400 and 'message to edit not found' in description.lower()
        self.reason = safe_error_reason(description)
        super().__init__(f'Telegram request failed (code {code}): {self.reason}')


def safe_error_reason(description):
    """Only emit fixed labels; Telegram error text may contain private content."""
    text = description.lower()
    reasons = [
        ('message is not modified', 'Message already has the requested content'),
        ('message to edit not found', 'Status message was deleted or is inaccessible'),
        ('upgraded to a supergroup', 'Group upgraded to a supergroup; its saved chat ID needs updating'),
        ('message to copy not found', 'Original message is missing or inaccessible to the bot'),
        ('message to forward not found', 'Original message is missing or inaccessible to the bot'),
        ('message can\'t be copied', 'Telegram does not allow this message to be copied'),
        ('protected', 'Source content is protected from copying or forwarding'),
        ('reply message not found', 'The message being replied to is no longer available'),
        ('message to be replied not found', 'The message being replied to is no longer available'),
        ('chat not found', 'Destination group is missing or inaccessible to the bot'),
        ('not enough rights', 'Bot lacks permission to send this content in the destination group'),
        ('have no rights', 'Bot lacks permission to send this content in the destination group'),
        ('chat_write_forbidden', 'Bot is not allowed to send messages in the destination group'),
        ('caption is too long', 'Assignment caption exceeds the Telegram length limit'),
        ('message is too long', 'Message exceeds the Telegram length limit'),
        ('parse entities', 'Telegram rejected the message formatting'),
        ('query is too old', 'Button response expired; tap the button again'),
        ('file is too big', 'File exceeds the Telegram API size limit'),
        ('bot was kicked', 'Bot was removed from the group'),
        ('bot is not a member', 'Bot is not a member of the group'),
    ]
    return next((label for fragment, label in reasons if fragment in text),
                'Unclassified Telegram rejection; check group access and the requested content')


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
            with urlopen(req, timeout=5 if method == 'answerCallbackQuery' else 40) as response:
                result = json.load(response)
        except HTTPError as exc:
            try:
                result = json.loads(exc.read())
            except (ValueError, OSError):
                raise TelegramError(exc.code) from None
        except (URLError, TimeoutError, OSError):
            raise TelegramError(0) from None
        if not result.get('ok'):
            params = result.get('parameters', {})
            raise TelegramError(result.get('error_code', 0), params.get('retry_after', 0), result.get('description', ''), params.get('migrate_to_chat_id'))
        return result['result']
