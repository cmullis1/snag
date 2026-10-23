"""The Snag extension's fixed identity. The manifest's "key" is EXTENSION_PUBLIC_KEY, so Chrome and Edge
give the unpacked extension the ID EXTENSION_ID, and the server allows chrome-extension://EXTENSION_ID
as an origin. Made once by scripts/make_extension_key.py."""
from __future__ import annotations

import base64
import binascii
import hashlib

EXTENSION_PUBLIC_KEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAmz6eMVIq+ou0eM1FNMrajPMif3wpevXk7CQQM8vONue8C0vFfu0W"
    "3rZDZAdVkzlEVuqFworfrkgqTs0/U9aq2ZCgPkwwx3q0ZMzlTtxS9tjsqqFvWBStPsYzGIJtVkOCXWeurRmdpk9mIWvyI8sV"
    "qAFrb+x5oHcmKwNp/XPesOCw6v/BZa9mFv4cPkuWPZG/ei9V+Qi+pEvYcFZ9nwI9SHrFsgFweUdhWKIAADeL1WQ2ecs++m0U"
    "/dzxkniwccfFIie1f2mYq49iXL9Yz7DPK2xoP0K2TjLAzfLgUmVHpuCnitdaoKFsPYE61b6Q6zYX40yKwQi9trXfUe7Tur52"
    "PwIDAQAB"
)
EXTENSION_ID = "jpmdepemlhgipdkeancolccbfjeogjko"

# Chrome writes an ID's hex digits 0-f as the letters a-p.
_HEX_TO_ID = str.maketrans("0123456789abcdef", "abcdefghijklmnop")


def extension_id_from_key(public_key_b64: str) -> str:
    """The ID Chrome gives an extension whose manifest "key" is public_key_b64 (base64 DER): the first
    32 hex digits of the SHA-256 of the DER bytes, mapped 0-f -> a-p. Raises ValueError for text that
    isn't base64."""
    try:
        der = base64.b64decode(public_key_b64, validate=True)
    except binascii.Error as error:
        raise ValueError(f"the extension key isn't valid base64: {error}") from error
    return hashlib.sha256(der).hexdigest()[:32].translate(_HEX_TO_ID)
