from cryptography.fernet import Fernet


class Crypto:
    def __init__(self, master_key: str):
        self._fernet = Fernet(master_key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()
