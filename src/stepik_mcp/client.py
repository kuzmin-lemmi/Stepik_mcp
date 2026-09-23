"""Минимальный клиент Stepik API.

Чтение идёт только через GET; единственный POST — получение OAuth-токена.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("STEPIK_BASE_URL", "https://stepik.org").rstrip("/")
TIMEOUT = int(os.environ.get("STEPIK_TIMEOUT", "30"))

# Ограничение на число id в одном запросе: Stepik отбивает слишком длинный заголовок.
IDS_CHUNK = 20


class StepikError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class StepikClient:
    """Минимальный клиент Stepik API. Только GET."""

    def __init__(self, client_id, client_secret, base_url=BASE_URL):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url
        self._token = None
        self._expires_at = 0.0

    # -- авторизация -------------------------------------------------------

    def _fetch_token(self):
        creds = f"{self.client_id}:{self.client_secret}".encode()
        req = urllib.request.Request(
            f"{self.base_url}/oauth2/token/",
            data=b"grant_type=client_credentials",
            headers={
                "Authorization": "Basic " + base64.b64encode(creds).decode(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise StepikError(
                f"Stepik отклонил client_credentials ({exc.code}). "
                "Проверь CLIENT_ID/CLIENT_SECRET и что у приложения "
                "Client type = Confidential, Grant type = Client credentials.",
                status=401,
            ) from exc
        except urllib.error.URLError as exc:
            raise StepikError(f"Не достучаться до Stepik: {exc.reason}") from exc

        self._token = payload["access_token"]
        # Обновляемся заранее, чтобы не поймать протухший токен на середине обхода.
        self._expires_at = time.time() + int(payload.get("expires_in", 36000)) - 300

    def token(self):
        if not self._token or time.time() >= self._expires_at:
            self._fetch_token()
        return self._token

    # -- запросы -----------------------------------------------------------

    def get(self, endpoint, **params):
        """Один GET. Возвращает распарсенный JSON целиком (с meta)."""
        query = []
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                query.extend((f"{key}[]", str(item)) for item in value)
            else:
                query.append((key, str(value)))
        url = f"{self.base_url}/api/{endpoint}"
        if query:
            url += "?" + urllib.parse.urlencode(query)

        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + self.token()}
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise StepikError(
                    f"403 на {endpoint}. Скорее всего у аккаунта, которому "
                    "принадлежит OAuth-приложение, нет авторских прав на этот курс.",
                    status=403,
                ) from exc
            raise StepikError(f"Stepik вернул {exc.code} на {endpoint}", status=502) from exc
        except urllib.error.URLError as exc:
            raise StepikError(f"Сеть до Stepik: {exc.reason}") from exc

    def collect(self, endpoint, key=None, max_pages=50, **params):
        """GET с проходом по всем страницам. Возвращает плоский список объектов."""
        key = key or endpoint
        out = []
        page = 1
        while page <= max_pages:
            payload = self.get(endpoint, page=page, **params)
            out.extend(payload.get(key, []))
            if not payload.get("meta", {}).get("has_next"):
                break
            page += 1
        return out

    def by_ids(self, endpoint, ids, key=None):
        """Батч-запрос по списку id. Пагинации в запросах с ids[] нет."""
        key = key or endpoint
        ids = [i for i in ids if i]
        out = []
        for start in range(0, len(ids), IDS_CHUNK):
            chunk = ids[start:start + IDS_CHUNK]
            out.extend(self.get(endpoint, ids=chunk).get(key, []))
        return out


# --------------------------------------------------------------------------
# Общие помощники
# --------------------------------------------------------------------------

def first_value(obj, *names):
    """Return the first non-None field from a dict."""
    if not isinstance(obj, dict):
        return None
    for name in names:
        if name in obj and obj[name] is not None:
            return obj[name]
    return None


def collect_limited(client, endpoint, key=None, max_items=1000, max_pages=50, **params):
    """Paginated GET with a hard item cap so a tool call cannot explode in size."""
    key = key or endpoint
    out = []
    page = 1
    truncated = False
    while page <= max_pages:
        payload = client.get(endpoint, page=page, **params)
        batch = payload.get(key, []) or []
        room = max(max_items - len(out), 0)
        if room <= 0:
            truncated = True
            break
        out.extend(batch[:room])
        if len(batch) > room:
            truncated = True
            break
        meta = payload.get("meta", {}) or {}
        if not meta.get("has_next"):
            break
        page += 1
    if page > max_pages:
        truncated = True
    return out, truncated
