import os
from dataclasses import dataclass


@dataclass
class Config:
    bot_token: str
    master_key: str
    allowed_user_ids: set[int]
    default_model: str | None
    db_path: str
    stt_api_key: str | None
    stt_base_url: str
    stt_model: str


def load_config() -> Config:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    master_key = os.environ.get("MASTER_KEY", "").strip()
    if not master_key:
        raise RuntimeError(
            "MASTER_KEY is not set. Generate one with: "
            "python3 -c \"import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())\""
        )

    raw_ids = os.environ.get("ALLOWED_USER_IDS", "").replace(";", ",")
    allowed = {int(x) for x in raw_ids.split(",") if x.strip()}
    if not allowed:
        raise RuntimeError(
            "ALLOWED_USER_IDS is empty. The bot holds SSH keys, an open bot is not allowed. "
            "Set a comma-separated list of Telegram user IDs."
        )

    return Config(
        bot_token=token,
        master_key=master_key,
        allowed_user_ids=allowed,
        default_model=os.environ.get("DEFAULT_MODEL", "").strip() or None,
        db_path=os.environ.get("DB_PATH", "/data/bot.db"),
        # Optional voice transcription via any OpenAI-compatible STT endpoint.
        # Defaults target Groq Whisper; set STT_API_KEY to switch it on.
        stt_api_key=os.environ.get("STT_API_KEY", "").strip() or None,
        stt_base_url=(os.environ.get("STT_BASE_URL", "").strip()
                      or "https://api.groq.com/openai/v1"),
        stt_model=(os.environ.get("STT_MODEL", "").strip()
                   or "whisper-large-v3-turbo"),
    )
