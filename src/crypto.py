"""加密层：Fernet 对称加密 + 主密钥管理。

主密钥来源优先级：
  1. .env 的 BUTLER_MASTER_PASSWORD（便于自动化）
  2. 系统密钥链（Windows Credential Locker，via keyring）
  3. 交互式输入并存入密钥链

密钥不落盘明文。数据加密用从主密码派生的密钥（PBKDF2）。
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

_KEYRING_SERVICE = "butler-agent"
_KEYRING_USER = "master"
# 盐固定存于此（非机密；PBKDF2 的盐可公开，作用是防彩虹表）
_SALT = b"butler-agent-v1-static-salt-change-if-you-like"
_PBKDF2_ROUNDS = 200_000


def _derive_key(password: str) -> bytes:
    """从主密码派生 32 字节 Fernet 密钥。"""
    raw = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), _SALT, _PBKDF2_ROUNDS, dklen=32
    )
    return base64.urlsafe_b64encode(raw)


def _get_master_password() -> str:
    # 1. 环境变量
    pw = os.environ.get("BUTLER_MASTER_PASSWORD")
    if pw:
        return pw

    # 2. 系统密钥链
    try:
        import keyring

        stored = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
        if stored:
            return stored

        # 3. 交互式设置并保存
        pw = getpass.getpass("首次运行，请设置管家主密码（用于加密本地数据）: ")
        if not pw:
            raise ValueError("主密码不能为空")
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, pw)
        print("✓ 主密码已存入系统密钥链，后续无需再输入。")
        return pw
    except ImportError:
        # keyring 不可用时退化为交互输入（不持久化）
        return getpass.getpass("请输入管家主密码: ")


class Cipher:
    """加解密工具。单例化以复用派生密钥。"""

    _instance: "Cipher | None" = None

    def __init__(self, password: str | None = None):
        pw = password or _get_master_password()
        self._fernet = Fernet(_derive_key(pw))

    @classmethod
    def instance(cls) -> "Cipher":
        if cls._instance is None:
            cls._instance = Cipher()
        return cls._instance

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken:
            raise ValueError("解密失败：主密码错误或数据被篡改")

    def encrypt_bytes(self, data: bytes) -> bytes:
        return self._fernet.encrypt(data)

    def decrypt_bytes(self, token: bytes) -> bytes:
        try:
            return self._fernet.decrypt(token)
        except InvalidToken:
            raise ValueError("解密失败：主密码错误或数据被篡改")
