import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


def load_env(path='.env'):
    if Path(path).exists():
        for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('\"').strip("'"))


@dataclass
class Config:
    token: str = ''
    admins: tuple[int, ...] = ()
    uploaders: int = 0
    editors: int = 0
    timezone: str = 'Asia/Kolkata'
    mode: str = 'effort'
    max_active: int = 2
    deadline_hours: int = 24
    approval: bool = True
    report_hour: int = 9
    database: str = 'data/bot.sqlite3'
    database_url: str = ''
    team_groups: dict = field(default_factory=dict)
    test_editor_id: int = 0
    test_mode_auto: bool = False

    @property
    def tz(self):
        return ZoneInfo(self.timezone)

    @classmethod
    def from_env(cls):
        load_env()
        c = cls(
            team_groups=json.loads(Path(__file__).with_name('team_groups.json').read_text(encoding='utf-8')),
            token=os.getenv('TELEGRAM_BOT_TOKEN', '').strip(),
            admins=tuple(int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()),
            uploaders=int(os.getenv('UPLOADERS_CHAT_ID') or 0),
            editors=int(os.getenv('EDITORS_CHAT_ID') or 0),
            timezone=os.getenv('TIMEZONE', 'Asia/Kolkata'),
            mode=os.getenv('ASSIGNMENT_MODE', 'effort'),
            max_active=int(os.getenv('MAX_ACTIVE_JOBS', '2')),
            deadline_hours=int(os.getenv('DEADLINE_HOURS', '24')),
            approval=os.getenv('REQUIRE_APPROVAL', 'true').lower() == 'true',
            report_hour=int(os.getenv('REPORT_HOUR', '9')),
            database=os.getenv('DATABASE_PATH', 'data/bot.sqlite3'),
            database_url=os.getenv('DATABASE_URL', '').strip(),
            test_editor_id=int(os.getenv('TEST_EDITOR_ID') or 0),
        )
        if c.mode not in ('effort', 'rotation') or c.max_active < 1 or c.deadline_hours < 1:
            raise ValueError('Invalid assignment mode, capacity, or deadline in .env')
        if not 0 <= c.report_hour <= 23:
            raise ValueError('REPORT_HOUR must be between 0 and 23')
        if c.uploaders and c.uploaders == c.editors:
            raise ValueError('The two groups must be different')
        if os.getenv('RENDER') and not c.database_url:
            raise ValueError('Render requires DATABASE_URL for durable Postgres storage; local SQLite is not safe on its free plan')
        if c.database_url and not c.database_url.startswith(('postgres://', 'postgresql://')):
            raise ValueError('DATABASE_URL must be a Postgres connection URL')
        test_settings = Path(__file__).with_name('test_mode.json')
        c.test_mode_auto = not c.test_editor_id and test_settings.exists() and bool(json.loads(test_settings.read_text()).get('enabled'))
        if c.test_mode_auto and len(c.admins) == 1:
            c.test_editor_id = c.admins[0]
        # Validate active test mode after loading its persisted disabled flag.
        c.tz  # Fail before starting if timezone data is missing.
        return c
