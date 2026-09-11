from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet


class SecretCipher:
    """使用本地 master key 加解密需要落库保存的敏感值。"""

    def __init__(self, master_key: str) -> None:
        digest = hashlib.sha256(master_key.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        """加密明文后返回适合存入 Metadata 数据库的文本密文。"""

        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, encrypted_value: str) -> str:
        """解密数据库中的密文；调用方不得把结果写入日志或 API 响应。"""

        return self._fernet.decrypt(encrypted_value.encode("utf-8")).decode("utf-8")
