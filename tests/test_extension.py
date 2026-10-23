import base64
import hashlib
import importlib.util
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_der_public_key

from snag.server.extension import EXTENSION_ID, EXTENSION_PUBLIC_KEY, extension_id_from_key

SCRIPT = Path(__file__).parent.parent / "scripts" / "make_extension_key.py"


def load_script():
    spec = importlib.util.spec_from_file_location("make_extension_key", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def chrome_id(der: bytes) -> str:
    """Chrome's rule, written out independently: SHA-256 of the key's DER bytes, the first 16 bytes as
    hex, each hex digit 0-f shifted to a letter a-p."""
    digest = hashlib.sha256(der).digest()[:16]
    letters = "abcdefghijklmnop"
    return "".join(letters[byte >> 4] + letters[byte & 0x0F] for byte in digest)


def test_the_committed_id_matches_the_committed_key():
    assert extension_id_from_key(EXTENSION_PUBLIC_KEY) == EXTENSION_ID


def test_the_committed_key_is_an_rsa_2048_public_key():
    key = load_der_public_key(base64.b64decode(EXTENSION_PUBLIC_KEY))
    assert isinstance(key, rsa.RSAPublicKey)
    assert key.key_size == 2048


def test_the_id_is_32_letters_from_a_to_p():
    assert len(EXTENSION_ID) == 32
    assert set(EXTENSION_ID) <= set("abcdefghijklmnop")


def test_known_vector():
    der = bytes(range(256)) * 2  # any fixed bytes: the ID depends only on their hash
    assert extension_id_from_key(base64.b64encode(der).decode("ascii")) == chrome_id(der)


def test_known_vector_with_a_digest_full_of_high_digits():
    # SHA-256 of b"" starts e3b0c442..., so the mapping of every digit, letters included, is exercised.
    assert extension_id_from_key("") == "odlameecjipmbmbejkplpemijjgpljce"
    assert chrome_id(b"") == "odlameecjipmbmbejkplpemijjgpljce"


@pytest.mark.parametrize("text", ["not base64!", "QUJD=x"])
def test_malformed_key_is_rejected(text):
    with pytest.raises(ValueError):
        extension_id_from_key(text)


def test_script_makes_a_key_whose_id_follows_chromes_rule():
    public_key_b64, extension_id = load_script().make_key()
    der = base64.b64decode(public_key_b64)
    assert load_der_public_key(der).key_size == 2048
    assert extension_id == chrome_id(der)


def test_script_prints_the_key_and_id_and_writes_no_files(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    load_script().main()
    printed = capsys.readouterr().out
    assert "EXTENSION_PUBLIC_KEY" in printed and "EXTENSION_ID" in printed
    assert list(tmp_path.iterdir()) == []
