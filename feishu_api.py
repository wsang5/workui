"""飞书 Open API 客户端 — 替换 lark-cli 子进程调用，直接通过 HTTP 调用飞书 API。"""

import json
import time
import threading
import urllib.request
import urllib.error
from urllib.parse import urlencode


BASE_URL = "https://open.feishu.cn/open-apis"
TOKEN_URL = f"{BASE_URL}/auth/v3/tenant_access_token/internal"


class FeishuClient:
    def __init__(self, app_id=None, app_secret=None):
        self.app_id = app_id or _env("FEISHU_APP_ID")
        self.app_secret = app_secret or _env("FEISHU_APP_SECRET")
        if not self.app_id or not self.app_secret:
            raise ValueError("请设置环境变量 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        self._token = None
        self._token_expires_at = 0
        self._lock = threading.Lock()

    # ── auth ──────────────────────────────────────────────

    def _ensure_token(self):
        now = time.time()
        if self._token and self._token_expires_at > now + 60:
            return self._token
        with self._lock:
            if self._token and self._token_expires_at > now + 60:
                return self._token
            data = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode()
            req = urllib.request.Request(TOKEN_URL, data=data, headers={"Content-Type": "application/json"})
            resp = _fetch(req)
            self._token = resp["tenant_access_token"]
            self._token_expires_at = now + resp.get("expire", 7200)
            return self._token

    def _request(self, method, path, body=None, params=None, raw=False):
        url = f"{BASE_URL}{path}"
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"
        headers = {
            "Authorization": f"Bearer {self._ensure_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        if raw:
            return _fetch_raw(req)
        return _fetch(req)

    # ── sheets ────────────────────────────────────────────

    def sheets_info(self, spreadsheet_token):
        """GET /sheets/v3/spreadsheets/{token}"""
        return self._request("GET", f"/sheets/v3/spreadsheets/{spreadsheet_token}")

    def sheets_query(self, spreadsheet_token):
        """GET /sheets/v3/spreadsheets/{token}/sheets/query"""
        return self._request("GET", f"/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query")

    def sheets_values_get(self, spreadsheet_token, sheet_id, range_str):
        """GET /sheets/v2/spreadsheets/{token}/values/{sheet_id}!{range}"""
        full_range = f"{sheet_id}!{range_str}" if sheet_id else range_str
        params = {"majorDimension": "ROWS", "valueRenderOption": "ToString", "dateTimeRenderOption": "FormattedString"}
        return self._request("GET", f"/sheets/v2/spreadsheets/{spreadsheet_token}/values/{full_range}", params=params)

    def sheets_values_set(self, spreadsheet_token, sheet_id, range_str, values):
        """PUT /sheets/v2/spreadsheets/{token}/values"""
        full_range = f"{sheet_id}!{range_str}" if sheet_id else range_str
        body = {"valueRange": {"range": full_range, "values": values}}
        return self._request("PUT", f"/sheets/v2/spreadsheets/{spreadsheet_token}/values", body=body)

    def sheets_batch_update(self, spreadsheet_token, requests):
        """POST /sheets/v2/spreadsheets/{token}/sheets_batch_update"""
        return self._request("POST", f"/sheets/v2/spreadsheets/{spreadsheet_token}/sheets_batch_update", body={"requests": requests})

    def sheets_values_image(self, spreadsheet_token, payload):
        """POST /sheets/v2/spreadsheets/{token}/values_image"""
        return self._request("POST", f"/sheets/v2/spreadsheets/{spreadsheet_token}/values_image", body=payload)

    def sheets_dimension_range(self, spreadsheet_token, payload):
        """PUT /sheets/v2/spreadsheets/{token}/dimension_range"""
        return self._request("PUT", f"/sheets/v2/spreadsheets/{spreadsheet_token}/dimension_range", body=payload)

    # ── bitable ───────────────────────────────────────────

    def bitable_app(self, app_token):
        """GET /bitable/v1/apps/{app_token}"""
        return self._request("GET", f"/bitable/v1/apps/{app_token}")

    def bitable_tables(self, app_token, page_size=100, page_token=None):
        """GET /bitable/v1/apps/{app_token}/tables"""
        params = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        return self._request("GET", f"/bitable/v1/apps/{app_token}/tables", params=params)

    def bitable_fields(self, app_token, table_id, page_size=100, page_token=None):
        """GET /bitable/v1/apps/{app_token}/tables/{table_id}/fields"""
        params = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        return self._request("GET", f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields", params=params)

    def bitable_records(self, app_token, table_id, page_size=200, page_token=None, view_id=None):
        """GET /bitable/v1/apps/{app_token}/tables/{table_id}/records"""
        params = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        if view_id:
            params["view_id"] = view_id
        return self._request("GET", f"/bitable/v1/apps/{app_token}/tables/{table_id}/records", params=params)

    def bitable_batch_update(self, app_token, table_id, records):
        """POST /bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update"""
        return self._request("POST", f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update", body={"records": records})

    # ── drive ─────────────────────────────────────────────

    def drive_media_download(self, file_token):
        """GET /drive/v1/medias/{file_token}/download — 返回 (bytes, content_type)"""
        return self._request("GET", f"/drive/v1/medias/{file_token}/download", raw=True)


# ── helpers ──────────────────────────────────────────────

def _env(name):
    import os
    return os.environ.get(name, "")


def _fetch(req):
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode("utf-8"))
        raise RuntimeError(f"飞书 API 错误 [{e.code}]: {body.get('msg', str(e))}")
    if body.get("code") != 0:
        raise RuntimeError(f"飞书 API 错误 [{body.get('code')}]: {body.get('msg', '')}")
    return body.get("data", {})


def _fetch_raw(req):
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read(), resp.headers.get("Content-Type", "application/octet-stream")
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode("utf-8"))
        raise RuntimeError(f"飞书 API 错误 [{e.code}]: {body.get('msg', str(e))}")


def spreadsheet_token_from_url(url):
    from urllib.parse import urlparse
    return urlparse(url).path.rstrip("/").split("/")[-1]
