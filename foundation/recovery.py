"""Recovery keys for an encrypted data volume, made on the client's side so the service never has to send a secret back.

The format is the one systemd uses for recovery keys: 32 random bytes written with the 16 letters of its alphabet, in eight blocks
of eight (`cbdefghi-jklnrtuv-...`). It works wherever a passphrase is accepted: typed at the unlock prompt, with the dashes.
"""

import re
import secrets

ALPHABET = "cbdefghijklnrtuv"
PATTERN = re.compile("^[" + ALPHABET + "]{8}(-[" + ALPHABET + "]{8}){7}$")


def new_recovery_key():
    letters = "".join(ALPHABET[byte >> 4] + ALPHABET[byte & 15] for byte in secrets.token_bytes(32))
    return "-".join(letters[i:i + 8] for i in range(0, 64, 8))


def is_recovery_key(text):
    return bool(PATTERN.match(text or ""))
