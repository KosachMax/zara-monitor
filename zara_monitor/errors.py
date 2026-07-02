from __future__ import annotations


class ConfigError(Exception):
    pass


class StorageError(Exception):
    pass


class StorageCorruptedError(StorageError):
    pass


class StorageValidationError(StorageError):
    pass


class MaxTrackedItemsError(StorageError):
    pass


class ZaraError(Exception):
    pass


class ZaraProductNotFound(ZaraError):
    pass


class ZaraRateLimited(ZaraError):
    pass


class ZaraTemporaryError(ZaraError):
    pass


class ZaraRequestError(ZaraError):
    pass


class TelegramError(Exception):
    pass


class TelegramConflictError(TelegramError):
    pass


class TelegramRateLimitedError(TelegramError):
    pass


class TelegramRequestError(TelegramError):
    pass
