"""Encryption at rest for the Zerodha login password and TOTP secret.

Why this exists: the nightly backup job snapshots the whole of ``oracle.db``
and uploads it to Google Drive (see ``downloader/services/backup.py``). An
enctoken in there expires within the day, but a Kite password and TOTP secret
together are a permanent, complete account takeover — those must never leave
the host in readable form.

So the two fields are encrypted with a key that lives **outside** the database:

1. ``ORACLE_SECRET_KEY`` in the environment, if set (a Fernet key, or any
   passphrase — it is stretched into one), or
2. ``data/secret.key``, generated on first use with 0600 permissions.

``data/`` is gitignored and is not part of the Drive backup, so a leaked
snapshot yields ciphertext and nothing else. Keep a copy of the key file
somewhere safe: restore a backup without it and the stored credentials are
unrecoverable (re-enter them in the app — nothing else is lost).

Encryption is Fernet (AES-128-CBC + HMAC-SHA256) from ``cryptography``. If that
package isn't installed the module still imports and reports
``available() is False``; callers then refuse to store secrets rather than
quietly writing them in the clear.
"""
import base64
import hashlib
import os

_KEY_ENV = "ORACLE_SECRET_KEY"
_KEY_FILE = os.path.join("data", "secret.key")

# Marks a value this module produced, so a plaintext leftover from an older
# database is recognisable rather than being fed to the decrypter as garbage.
_PREFIX = "enc:"


def _fernet_class():
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    return Fernet


def available():
    """True when secrets can actually be encrypted (``cryptography`` present)."""
    return _fernet_class() is not None


def install_hint():
    return ("Encrypted storage needs the `cryptography` package: "
            "`venv/bin/pip install -r requirements.txt`, then restart the app.")


def _derive(passphrase):
    """Stretch an arbitrary passphrase into a Fernet key.

    A real Fernet key (32 url-safe base64 bytes) is used as-is; anything else is
    hashed so a human-typed value still works.
    """
    raw = passphrase.strip()
    try:
        if len(base64.urlsafe_b64decode(raw)) == 32:
            return raw.encode()
    except (ValueError, TypeError):
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())


def _key_path():
    return os.environ.get("ORACLE_SECRET_KEY_FILE", _KEY_FILE)


def _load_or_create_key():
    """The encryption key, from the environment or the key file (created once)."""
    from_env = os.environ.get(_KEY_ENV, "").strip()
    if from_env:
        return _derive(from_env)

    path = _key_path()
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read().strip()

    Fernet = _fernet_class()
    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # Written 0600 before anything goes in it: the file is the whole secret.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def _cipher():
    Fernet = _fernet_class()
    if Fernet is None:
        raise RuntimeError(install_hint())
    return Fernet(_load_or_create_key())


def encrypt(value):
    """Encrypt a secret for storage. Empty input stays empty (means "not set")."""
    text = (value or "").strip()
    if not text:
        return ""
    return _PREFIX + _cipher().encrypt(text.encode()).decode()


def decrypt(stored):
    """Read a stored secret back.

    Returns ``""`` when nothing is stored, and ``None`` when a value exists but
    cannot be read — a rotated or missing key, or a corrupt snapshot. ``None``
    is deliberately distinct from ``""`` so the UI can say "there is a secret
    here that this host can no longer decrypt" instead of "no secret set".
    """
    text = (stored or "").strip()
    if not text:
        return ""
    if not text.startswith(_PREFIX):
        # Written before encryption existed (or by hand). Readable as-is; the
        # next save re-writes it encrypted.
        return text
    if not available():
        return None
    from cryptography.fernet import InvalidToken
    try:
        return _cipher().decrypt(text[len(_PREFIX):].encode()).decode()
    except (InvalidToken, ValueError, RuntimeError):
        return None


def is_encrypted(stored):
    return bool((stored or "").strip().startswith(_PREFIX))
