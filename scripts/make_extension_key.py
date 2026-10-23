"""Makes the key pair that fixes the Snag extension's ID (run once; the output is committed).

Chrome derives an unpacked extension's ID from the public key in its manifest's "key", so a fixed key
gives a fixed ID, which the server allows as an origin (snag/server/extension.py). Prints the public key
(base64 DER SubjectPublicKeyInfo) and its ID. The private key is never written anywhere: an unpacked
extension doesn't need it, and only packing one for a store would.

    .venv/Scripts/python scripts/make_extension_key.py
"""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from snag.server.extension import extension_id_from_key


def make_key() -> tuple[str, str]:
    """A new RSA-2048 public key as base64 DER, and the extension ID it gives."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = private_key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    public_key_b64 = base64.b64encode(der).decode("ascii")
    return public_key_b64, extension_id_from_key(public_key_b64)


def main() -> None:
    public_key_b64, extension_id = make_key()
    print(f'EXTENSION_PUBLIC_KEY = "{public_key_b64}"')
    print(f'EXTENSION_ID = "{extension_id}"')


if __name__ == "__main__":
    main()
