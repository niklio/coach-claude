import json
import os
import threading

_lock = threading.Lock()
_TOKEN_PATH = os.getenv("TOKEN_PATH", "tokens.json")


def load_tokens() -> dict | None:
    with _lock:
        if not os.path.exists(_TOKEN_PATH):
            # Bootstrap from env vars (useful on Railway where filesystem is ephemeral)
            access = os.getenv("STRAVA_ACCESS_TOKEN")
            refresh = os.getenv("STRAVA_REFRESH_TOKEN")
            if access and refresh:
                tokens = {"access_token": access, "refresh_token": refresh, "expires_at": 0}
                with open(_TOKEN_PATH, "w") as f:
                    json.dump(tokens, f, indent=2)
                return tokens
            return None
        with open(_TOKEN_PATH) as f:
            return json.load(f)


def save_tokens(token_dict: dict) -> None:
    with _lock:
        with open(_TOKEN_PATH, "w") as f:
            json.dump(token_dict, f, indent=2)
