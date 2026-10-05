"""Single owner bootstrap from trusted Telegram updates, persisted in SQLite."""

from .config import Settings
from .repository import Repository


def telegram_owner_ids(repo: Repository, settings: Settings) -> set[int]:
    # An explicit operator allowlist overrides a previously claimed owner.
    if settings.telegram_owner_ids:
        return set(settings.telegram_owner_ids)
    owner = repo.get_setting("telegram_claimed_owner")
    return {owner} if type(owner) is int and owner > 0 else set()


def claim_telegram_owner(repo: Repository, settings: Settings, owner: int) -> bool:
    """Return True only for the successful first claim; no HTTP/API bootstrap."""
    if not settings.telegram_claim_first_user or settings.telegram_owner_ids or type(owner) is not int or owner <= 0:
        return False
    with repo.transaction() as db:
        result = db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('telegram_claimed_owner',?)", (str(owner),))
        if result.rowcount != 1:
            return False
        repo.event("telegram_owner_claimed", owner_id=owner)
        return True
