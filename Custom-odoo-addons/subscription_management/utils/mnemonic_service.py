# utils/wallet_utils.py
import os, base64, json, hashlib, datetime
from typing import Dict, Optional, Any
import re
from cryptography.hazmat.primitives import hashes
from bip_utils import (
    Bip39MnemonicGenerator, Bip39WordsNum, Bip39MnemonicValidator, Bip39SeedGenerator,
    Bip32Slip10Secp256k1, Bip39Mnemonic
)
from cryptography.hazmat.primitives.asymmetric import padding
from odoo.exceptions import UserError
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad, unpad
from Crypto.Hash import RIPEMD160
from odoo import  _



HD_PATH = "m/44'/990'/0'/0/0"

# ---- Mnemonic ----

def _mnemonic_to_str(self, mnemonic) -> str:
        """Accept Bip39Mnemonic or str; always return str."""
        if isinstance(mnemonic, Bip39Mnemonic):
            return mnemonic.ToStr()
        if isinstance(mnemonic, str):
            return mnemonic
        raise UserError(_("Mnemonic must be a string or Bip39Mnemonic."))

def generate_mnemonic(self, words: int = 12) -> str:
    """Generate a BIP39 mnemonic; default 12 words (matches Node bip39 default)."""
    words_num = {
        12: Bip39WordsNum.WORDS_NUM_12,
        15: Bip39WordsNum.WORDS_NUM_15,
        18: Bip39WordsNum.WORDS_NUM_18,
        21: Bip39WordsNum.WORDS_NUM_21,
        24: Bip39WordsNum.WORDS_NUM_24,
    }.get(words)
    if not words_num:
        raise UserError(_("Invalid words number. Choose one of: 12, 15, 18, 21, 24."))
    return Bip39MnemonicGenerator().FromWordsNumber(words_num).ToStr()

def validate_mnemonic(self, mnemonic: str) -> bool:
    """Validate a BIP39 mnemonic (same as bip39.validateMnemonic)."""
    try:
        mn_str = _mnemonic_to_str(self,mnemonic)
        Bip39MnemonicValidator(mn_str).Validate()
        return True
    except Exception:
        return False

# ---- Address derivation ----
def _pubkey_to_coreum_address(self, compressed_pubkey_bytes: bytes, hrp: str) -> str:
    """Cosmos-style address: bech32(hrp, RIPEMD160(SHA256(pubkey)))"""
    sha = hashlib.sha256(compressed_pubkey_bytes).digest()
    ripemd = RIPEMD160.new(sha).digest()
    from bip_utils import Bech32Encoder
    return Bech32Encoder.Encode(hrp, ripemd)

def generate_mnemonic_and_address(self, testnet: bool = True, mnemonic: str | None = None) -> dict:
    """
    Generate (or accept) a mnemonic and derive Coreum address at m/44'/990'/0'/0/0.
    Returns: { 'botAddress': str, 'mnemonic': str }
    """
    # 1) Ensure mnemonic
    mn = _mnemonic_to_str(self,mnemonic) if mnemonic else generate_mnemonic(self,12)

    # 2) Seed
    seed_bytes = Bip39SeedGenerator(mn).Generate()

    # 3) Derive SLIP-10 secp256k1 at the Coreum path
    bip32_ctx = Bip32Slip10Secp256k1.FromSeedAndPath(seed_bytes, HD_PATH)
    pubkey_compressed = bip32_ctx.PublicKey().RawCompressed().ToBytes()

    # 4) Convert to bech32 Coreum address
    prefix = "testcore" if testnet else "core"
    address = _pubkey_to_coreum_address(self,pubkey_compressed, prefix)

    return {"address": address, "mnemonic": mn}

IV_LENGTH = 16  # AES block size

def _ensure_key_bytes(key: str | bytes) -> bytes:
    if isinstance(key, str):
        # If it's a hex string (common for 32-byte keys), decode it
        if len(key) == 64 and all(c in "0123456789abcdefABCDEF" for c in key):
            key_bytes = bytes.fromhex(key)
        else:
            key_bytes = key.encode("utf-8")
    else:
        key_bytes = key

    if len(key_bytes) not in [16, 24, 32]:
        raise ValueError(f"AES key must be 16, 24, or 32 bytes. Got {len(key_bytes)} bytes.")
    return key_bytes

def get_aes_key(env=None) -> str:
    """
    Retrieve the AES encryption key from the .env file.
    Falls back to ir.config_parameter if .env is not found or key is missing.
    """
    try:
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                for line in f:
                    if line.startswith('AES_ENCRYPTION_KEY='):
                        return line.split('=', 1)[1].strip()
    except Exception as e:
        print(f"Error reading .env: {e}")

    if env:
        return env['ir.config_parameter'].sudo().get_param('mnemonic_encryption_key')
    return None

def encrypt_aes(text: str, key: str | bytes) -> str:
    """General AES encryption: text -> iv:ciphertext (hex)"""
    if not text:
        return ""
    key_bytes = _ensure_key_bytes(key)
    iv = get_random_bytes(IV_LENGTH)
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(pad(text.encode("utf-8"), AES.block_size))
    return f"{iv.hex()}:{ciphertext.hex()}"

def decrypt_aes(token: str, key: str | bytes) -> str:
    """General AES decryption: iv:ciphertext (hex) -> text"""
    if not token:
        return ""
    try:
        key_bytes = _ensure_key_bytes(key)
        iv_hex, ct_hex = token.split(":", 1)
        iv = bytes.fromhex(iv_hex)
        ciphertext = bytes.fromhex(ct_hex)
        cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
        plaintext = unpad(cipher.decrypt(ciphertext), AES.block_size)
        return plaintext.decode("utf-8")
    except Exception as e:
        # Fallback for unencrypted data or wrong key
        return token

def encrypt_mnemonic(text: str, key: str | bytes) -> str:
    return encrypt_aes(text, key)

def decrypt_mnemonic(token: str, key: str | bytes) -> str:
    return decrypt_aes(token, key)

#-------------------------------------------------------------------------------
#-------- RSA encryption compatible with NodeRSA defaults (OAEP + SHA1) --------
#-------------------------------------------------------------------------------


def _normalize_pem(pem_raw: str) -> bytes:
    """Make sure PEM has proper newlines and only base64 in the body."""
    if not pem_raw:
        raise ValueError("Empty PEM")

    # Turn literal '\n' into real newlines (common in env/ir.config_parameter)
    pem = pem_raw.replace("\\n", "\n").strip()

    # If header/body/footer ended up on one line, split them
    # Match any BEGIN ... END ... with base64 in between
    m = re.match(r"^-----BEGIN ([A-Z0-9 ]+)-----([A-Za-z0-9+/=\s]+)-----END \1-----$", pem.replace("\r", ""), re.DOTALL)
    if m:
        typ, body = m.group(1), m.group(2)
        # Remove any whitespace in body, then wrap at 64-chars
        b64 = re.sub(r"\s+", "", body)
        wrapped = "\n".join([b64[i:i+64] for i in range(0, len(b64), 64)])
        pem = f"-----BEGIN {typ}-----\n{wrapped}\n-----END {typ}-----\n"
        return pem.encode("utf-8")

    # If it already looks good (has line breaks), just return bytes
    return pem.encode("utf-8")

def _load_public_key(pem_str: str):
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    return load_pem_public_key(_normalize_pem(pem_str))


def _load_private_key(pem_str: str, password: Optional[str] = None):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    pw_bytes = password.encode("utf-8") if password else None
    return load_pem_private_key(_normalize_pem(pem_str), password=pw_bytes)


def encrypt_data(env, data: Dict[str, Any]) -> str:
    """
    Encrypt dict -> base64, compatible with NodeRSA default (OAEP + SHA1).
    """
    try:
        ICP = env["ir.config_parameter"].sudo()
        public_pem = ICP.get_param("security_keys.rsa_public_key")
        if not public_pem:
            raise ValueError("Missing RSA public key in ir.config_parameter (key: security_keys.rsa_public_key)")
        public_key = _load_public_key(public_pem)
        plaintext = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        ciphertext = public_key.encrypt(
            plaintext,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA1()),
                algorithm=hashes.SHA1(),
                label=None,
            ),
        )
        return base64.b64encode(ciphertext).decode("ascii")
    except Exception as e:
        raise UserError(f"Encryption failed: {str(e)}")


def decrypt_data(env, encrypted_b64: str, password: str | None = None) -> Dict[str, Any]:
    """
    Decrypt base64 -> dict, compatible with NodeRSA default (OAEP + SHA1).
    """
    try:
        ICP = env["ir.config_parameter"].sudo()
        private_pem = ICP.get_param("security_keys.rsa_private_key")
        private_key = _load_private_key(private_pem, password=password)
        ciphertext = base64.b64decode(encrypted_b64)

        plaintext = private_key.decrypt(
            ciphertext,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA1()),
                algorithm=hashes.SHA1(),
                label=None,
            ),
        )
        return json.loads(plaintext.decode("utf-8"))
    except Exception as e:
        raise UserError(f"Decryption failed: {str(e)}")
