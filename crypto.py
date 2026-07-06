"""
crypto.py
---------
Fernet symmetric encryption for sensitive values stored in the database.
Used to encrypt DB connection URIs at rest.

The ENCRYPTION_KEY is a base64-encoded 32-byte key stored in the environment.
Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from cryptography.fernet import Fernet, InvalidToken
from config import cfg
import logging

logger = logging.getLogger(__name__)
_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = cfg.ENCRYPTION_KEY
        # Fernet needs a bytes key; handle both str and bytes
        if isinstance(key, str):
            key = key.encode()
        _fernet = Fernet(key)
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypts a string and returns a base64-encoded ciphertext string."""
    if not plaintext:
        return ""
    try:
        return _get_fernet().encrypt(plaintext.encode()).decode()
    except Exception as e:
        logger.error(f"[Crypto] Encryption failed: {e}")
        raise


def decrypt(ciphertext: str) -> str:
    """Decrypts a ciphertext string. Returns empty string if decryption fails."""
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.error("[Crypto] Decryption failed — wrong key or corrupted data")
        raise ValueError(
            "Could not decrypt connection URI. "
            "If you rotated ENCRYPTION_KEY, you need to re-add your connections."
        )
    except Exception as e:
        logger.error(f"[Crypto] Unexpected decryption error: {e}")
        raise


def is_encrypted(value: str) -> bool:
    """Heuristic check — Fernet tokens start with 'gAAAAA'."""
    return value.startswith("gAAAAA")