"""
Cross-check plugins/jlc_colorsilk.py's pure-Python crypto against `cryptography`.

Usage:
    py -m pip install cryptography
    py tools/jlc_crypto_check.py
"""
import os
import sys
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugins"))
import jlc_colorsilk as jc  # noqa: E402

for n in list(range(0, 70)) + [1000, 4097]:
    for iv_len in (12, 16):
        key, iv, data = os.urandom(16), os.urandom(iv_len), os.urandom(n)
        assert jc.aes_gcm_encrypt(key, iv, data) == AESGCM(key).encrypt(iv, data, None), (n, iv_len)
print("AES-GCM matches cryptography")

priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pub = priv.public_key().public_numbers()
oaep = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
for _ in range(20):
    msg = os.urandom(16)
    assert priv.decrypt(jc.rsa_oaep_encrypt(msg, pub.n, pub.e), oaep) == msg
print("RSA-OAEP decrypts with cryptography")

data = os.urandom(1 << 20)
t = time.perf_counter()
blob = jc.encrypt(data)
print("1 MiB encrypt: %.2f s, %d bytes" % (time.perf_counter() - t, len(blob)))
