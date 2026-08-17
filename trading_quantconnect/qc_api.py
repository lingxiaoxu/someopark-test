"""qc_api — QuantConnect API v2 客户端(镜像项目专用,零仓库内依赖)。

防火墙纪律: 本模块只消费 .env 的 QC 凭证,只与 QC 云通信;
绝不读写仓库内任何策略/生产文件。

认证: Basic base64(userId : sha256(token:timestamp)) + Timestamp 头。
所有调用返回 (ok: bool, data: dict);HTTP/网络异常统一抛 QcApiError
(调用方决定重试/终止 —— 不静默)。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path

import requests

BASE = "https://www.quantconnect.com/api/v2"
_THIS_DIR = Path(__file__).resolve().parent
_ENV_PATH = _THIS_DIR.parent / ".env"


class QcApiError(RuntimeError):
    pass


def _load_env_key(name: str) -> str:
    v = os.environ.get(name)
    if v:
        return v
    # .env 只读解析(不 source 整个环境,防火墙最小面)
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    raise QcApiError(f"{name} not found in env or {_ENV_PATH}")


class QcClient:
    def __init__(self, user_id: str | None = None, token: str | None = None,
                 timeout: int = 60):
        self.uid = user_id or _load_env_key("QC_USER_ID")
        self.token = token or _load_env_key("QC_API_TOKEN")
        self.timeout = timeout
        self.s = requests.Session()

    # ── 底层 ────────────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        ts = str(int(time.time()))
        h = hashlib.sha256(f"{self.token}:{ts}".encode()).hexdigest()
        auth = base64.b64encode(f"{self.uid}:{h}".encode()).decode()
        return {"Authorization": f"Basic {auth}", "Timestamp": ts}

    def call(self, path: str, payload: dict | None = None,
             retries: int = 3) -> dict:
        """→ data(success 校验后);失败抛 QcApiError 带 QC errors 原文。"""
        last = None
        for attempt in range(retries):
            try:
                r = self.s.post(f"{BASE}/{path}", headers=self._headers(),
                                json=payload or {}, timeout=self.timeout)
                d = r.json()
            except Exception as e:  # noqa: BLE001 — 网络层重试
                last = e
                time.sleep(2 ** attempt)
                continue
            if d.get("success"):
                return d
            # QC 侧业务失败: 不重试(参数错误重试无意义),原文上抛
            raise QcApiError(f"{path}: {d.get('errors') or d}")
        raise QcApiError(f"{path}: network failed after {retries}: {last}")

    # ── 组织/节点 ───────────────────────────────────────────────────────────
    def organization_id(self) -> str:
        d = self.call("organizations/list")
        orgs = d.get("organizations", [])
        pref = [o for o in orgs if o.get("preferred")] or orgs
        if not pref:
            raise QcApiError("no organization on account")
        return pref[0]["id"]

    def live_nodes(self, org_id: str) -> list[dict]:
        d = self.call("nodes/read", {"organizationId": org_id})
        return d.get("live") or []

    def idle_live_node_id(self, org_id: str) -> str:
        for n in self.live_nodes(org_id):
            if not n.get("busy"):
                return n["id"]
        raise QcApiError("no idle live node (L-MICRO busy? stop existing deploy)")

    # ── 项目/文件/编译 ───────────────────────────────────────────────────────
    def find_project(self, name: str) -> int | None:
        d = self.call("projects/read")
        for p in d.get("projects", []):
            if p.get("name") == name:
                return int(p["projectId"])
        return None

    def create_project(self, name: str, language: str = "Py") -> int:
        d = self.call("projects/create", {"name": name, "language": language})
        return int(d["projects"][0]["projectId"])

    def upsert_file(self, project_id: int, name: str, content: str) -> None:
        d = self.call("files/read", {"projectId": project_id})
        existing = {f["name"] for f in d.get("files", [])}
        if name in existing:
            self.call("files/update", {"projectId": project_id, "name": name,
                                       "content": content})
        else:
            self.call("files/create", {"projectId": project_id, "name": name,
                                       "content": content})

    def compile_project(self, project_id: int, poll_s: float = 2.0,
                        timeout_s: float = 180.0) -> str:
        d = self.call("compile/create", {"projectId": project_id})
        cid = d["compileId"]
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            d = self.call("compile/read", {"projectId": project_id,
                                           "compileId": cid})
            state = d.get("state")
            if state == "BuildSuccess":
                return cid
            if state == "BuildError":
                raise QcApiError(f"compile failed: {d.get('logs')}")
            time.sleep(poll_s)
        raise QcApiError("compile timeout")

    # ── live 部署 ───────────────────────────────────────────────────────────
    def live_create_paper(self, project_id: int, compile_id: str,
                          node_id: str) -> dict:
        payload = {
            "projectId": project_id,
            "compileId": compile_id,
            "nodeId": node_id,
            "versionId": "-1",
            "brokerage": {"id": "QuantConnectBrokerage"},
            "dataProviders": {"QuantConnectBrokerage":
                              {"id": "QuantConnectBrokerage"}},
        }
        return self.call("live/create", payload)

    def live_stop(self, project_id: int) -> dict:
        return self.call("live/update/stop", {"projectId": project_id})

    def live_liquidate(self, project_id: int) -> dict:
        return self.call("live/update/liquidate", {"projectId": project_id})

    def live_read(self, project_id: int) -> dict:
        return self.call("live/read", {"projectId": project_id})

    def live_portfolio(self, project_id: int) -> dict:
        return self.call("live/portfolio/read", {"projectId": project_id})

    def live_orders(self, project_id: int, start: int = 0,
                    end: int = 100) -> dict:
        return self.call("live/orders/read",
                         {"projectId": project_id, "start": start, "end": end})

    def live_logs(self, project_id: int, start_line: int = 0,
                  end_line: int = 250) -> dict:
        live = self.live_read(project_id)
        algo_id = live.get("deployId")
        return self.call("live/logs/read",
                         {"format": "json", "projectId": project_id,
                          "algorithmId": algo_id,
                          "startLine": start_line, "endLine": end_line})

    # ── ObjectStore(target 传输面)──────────────────────────────────────────
    def object_set(self, org_id: str, key: str, data: bytes) -> dict:
        """multipart 上传(object/set 走文件表单,非 json)。"""
        ts = str(int(time.time()))
        h = hashlib.sha256(f"{self.token}:{ts}".encode()).hexdigest()
        auth = base64.b64encode(f"{self.uid}:{h}".encode()).decode()
        r = self.s.post(f"{BASE}/object/set",
                        headers={"Authorization": f"Basic {auth}",
                                 "Timestamp": ts},
                        data={"organizationId": org_id, "key": key},
                        files={"objectData": ("objectData", data)},
                        timeout=self.timeout)
        d = r.json()
        if not d.get("success"):
            raise QcApiError(f"object/set {key}: {d.get('errors') or d}")
        return d

    def object_get(self, org_id: str, key: str) -> bytes | None:
        """object/get 是两步: 请求生成下载 url → 下载。不存在返回 None。"""
        try:
            d = self.call("object/get", {"organizationId": org_id,
                                         "keys": [key]})
        except QcApiError as e:
            if "not found" in str(e).lower():
                return None
            raise
        url = d.get("url")
        if not url:
            return None
        r = self.s.get(url, timeout=self.timeout)
        r.raise_for_status()
        # url 返回 zip(键→文件)或裸内容,调用方按 target 场景处理
        return r.content


if __name__ == "__main__":
    c = QcClient()
    oid = c.organization_id()
    print(json.dumps({"org": oid,
                      "live_nodes": [(n.get("name"), n.get("busy"))
                                     for n in c.live_nodes(oid)]},
                     ensure_ascii=False))
