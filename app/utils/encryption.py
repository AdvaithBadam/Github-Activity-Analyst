"""Symmetric encryption helpers using Fernet.

The encryption key is loaded from ``settings.ENCRYPTION_KEY``.

Generate a valid Fernet key with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from cryptography.fernet import Fernet

from app.config import settings

_fernet = Fernet(settings.ENCRYPTION_KEY.encode())


def encrypt_token(plain: str) -> str:
    """Encrypt a plaintext string and return a URL-safe base64 ciphertext."""
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    """Decrypt a Fernet ciphertext back to the original plaintext string."""
    return _fernet.decrypt(encrypted.encode()).decode()
