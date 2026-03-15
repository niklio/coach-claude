import json
import os
import threading

_lock = threading.Lock()
_TOKEN_PATH = os.getenv("TOKEN_PATH", "tokens.json")


def load_tokens() -> dict | None:
    with _lock:
        if not os.path.exists(_TOKEN_PATH):
            return None
        with open(_TOKEN_PATH) as f:
            return json.load(f)


def save_tokens(token_dict: dict) -> None:
    with _lock:
        with open(_TOKEN_PATH, "w") as f:
            json.dump(token_dict, f, indent=2)
