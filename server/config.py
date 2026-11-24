from dataclasses import dataclass, field
import os
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')


def project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return (ROOT / path).resolve() if not path.is_absolute() else path.resolve()


def flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {'1', 'true', 'yes', 'on'}


@dataclass
class Settings:
    storage_dir: Path = field(default_factory=lambda: project_path(os.getenv('STORAGE_DIR', os.getenv('SERVICE_STORAGE_ROOT', str(ROOT / 'storage')))))
    database_url: str = field(default_factory=lambda: os.getenv('DATABASE_URL') or os.getenv('DB_URL', ''))
    app_mode: str = field(default_factory=lambda: os.getenv('APP_MODE', 'SAFE').upper())
    cookie_secure: bool = field(default_factory=lambda: flag('COOKIE_SECURE'))
    session_days: int = 7
    max_file_bytes: int = 25 * 1024 * 1024
    openai_api_key: str = field(default_factory=lambda: os.getenv('OPENAI_API_KEY') or os.getenv('POSTEKS_OPENAI_API_KEY', ''))
    openai_model: str = field(default_factory=lambda: os.getenv('OPENAI_MODEL', os.getenv('OPENAI_MAIN_MODEL', 'gpt-4.1-mini')))
    openai_fast_model: str = field(default_factory=lambda: os.getenv('OPENAI_FAST_MODEL', 'gpt-4.1-mini'))
    openai_vision_model: str = field(default_factory=lambda: os.getenv('OPENAI_VISION_MODEL', 'gpt-4.1-mini'))
    openai_audio_model: str = field(default_factory=lambda: os.getenv('OPENAI_AUDIO_MODEL', os.getenv('OPENAI_TRANSCRIPTION_MODEL', 'gpt-4o-mini-transcribe')))
    openai_embedding_model: str = field(default_factory=lambda: os.getenv('OPENAI_EMBEDDING_MODEL', os.getenv('OPENAI_EMBED_MODEL', 'text-embedding-3-small')))
    web_search_enabled: bool = field(default_factory=lambda: flag('POSTEKS_WEB_SEARCH_ENABLED', True))
    web_search_domains: str = field(default_factory=lambda: os.getenv('POSTEKS_WEB_SEARCH_DOMAINS', 'veichi.com,posteks.ru'))
    public_base_url: str = field(default_factory=lambda: os.getenv('PUBLIC_BASE_URL', '').rstrip('/'))
    allowed_origins: str = field(default_factory=lambda: os.getenv('ALLOWED_ORIGINS', ''))
    max_ai_output_tokens: int = field(default_factory=lambda: int(os.getenv('MAX_AI_OUTPUT_TOKENS', '4096')))
    max_ai_daily_tokens: int = field(default_factory=lambda: int(os.getenv('MAX_AI_DAILY_TOKENS', '100000')))
    # Explicit offline fixtures only; ignored outside TEST. It never represents
    # a successful language-model integration.
    reference_test_mode: bool = field(default_factory=lambda: flag('POSTEKS_REFERENCE_TEST_MODE'))
    configuration_warnings: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not 256 <= self.max_ai_output_tokens <= 16384:
            raise ValueError('MAX_AI_OUTPUT_TOKENS must be between 256 and 16384')
        self.storage_dir = project_path(self.storage_dir)
        if os.getenv('STORAGE_DIR') and os.getenv('SERVICE_STORAGE_ROOT') and project_path(os.environ['STORAGE_DIR']) != project_path(os.environ['SERVICE_STORAGE_ROOT']):
            self.configuration_warnings.append('STORAGE_DIR и SERVICE_STORAGE_ROOT указывают на разные папки; используется STORAGE_DIR.')
        if os.getenv('DATABASE_URL') and os.getenv('DB_URL') and os.environ['DATABASE_URL'] != os.environ['DB_URL']:
            self.configuration_warnings.append('DATABASE_URL и DB_URL различаются; используется DATABASE_URL.')
        if self.app_mode not in {'TEST', 'SAFE', 'PRODUCTION'}:
            raise ValueError('APP_MODE must be TEST, SAFE or PRODUCTION')
        if not self.database_url:
            self.database_url = 'sqlite:///' + str(self.storage_dir / 'posteks_ai.db')
        database = make_url(self.database_url)
        if database.get_backend_name() == 'sqlite':
            if database.drivername == 'sqlite+aiosqlite':
                database = database.set(drivername='sqlite')
            # Every entry point resolves the same database, even when launched
            # from another working directory. Never silently select an empty DB.
            if database.database and database.database != ':memory:' and not database.database.startswith('file:'):
                database = database.set(database=str(project_path(database.database)))
            database_name = Path((database.database or '').replace('\\', '/')).name.casefold()
            if database_name == 'posteks.db':
                raise ValueError('Legacy posteks.db is protected. Use a separate database and explicit migration.')
            self.database_url = database.render_as_string(hide_password=False)
        for name in ('TESSDATA_PREFIX', 'TESSERACT_CMD'):
            if os.getenv(name) and (name == 'TESSDATA_PREFIX' or '/' in os.environ[name] or '\\' in os.environ[name]):
                os.environ[name] = str(project_path(os.environ[name]))
        if self.app_mode == 'PRODUCTION' and not self.cookie_secure:
            raise ValueError('PRODUCTION requires COOKIE_SECURE=true and HTTPS')

    @property
    def documents_dir(self) -> Path:
        return self.storage_dir / 'documents'


settings = Settings()


def effective_mode(session) -> str:
    """Persisted runtime mode; all integrations must use the same effective value."""
    from .models import AppSetting
    row = session.get(AppSetting, 'mode')
    return row.value.get('mode', settings.app_mode) if row else settings.app_mode
