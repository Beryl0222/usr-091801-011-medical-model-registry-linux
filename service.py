"""医疗影像模型准入台账的运行入口。

用法：
- python3 service.py --check                      基础检查（健康载荷 + 清单可加载）
- python3 service.py --port 8000 [--data-file f]  启动 HTTP 服务
- python3 service.py --evaluate SCENARIO          复算情景中的本地验证准入结论
- python3 service.py --replay-drift SCENARIO      回放反馈流，输出漂移处置
- python3 service.py --verify-calls SCENARIO      核对线上调用是否命中获批版本
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from registry import api
from registry.checklist import load_checklist
from registry.engine import run_scenario
from registry.store import RegistryStore

SERVICE_ID = "medical-model-registry"
SERVICE_NAME = "医疗影像模型准入台账"


def health_payload():
    """返回服务状态与身份。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """健康检查 + 台账 JSON 接口。"""

    store = None
    checklist = None
    data_file = None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, health_payload())
            return
        self._dispatch("GET", parsed)

    def do_POST(self):
        self._dispatch("POST", urlparse(self.path))

    def _dispatch(self, method, parsed):
        store = type(self).store or api.default_store()
        checklist = type(self).checklist or api.default_checklist()
        body = None
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send(400, {"error": "请求体不是合法 JSON"})
                    return
        headers = {key.lower(): value for key, value in self.headers.items()}
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        try:
            status, payload = api.dispatch(
                method, parsed.path, query, headers, body, store, checklist
            )
        except Exception:  # 服务边界兜底，不把内部异常暴露给调用方
            self._send(500, {"error": "服务内部错误"})
            return
        if method == "POST" and status < 300 and type(self).data_file:
            store.save(type(self).data_file)
        self._send(status, payload)

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def _run_scenario_command(args):
    scenario_path = args.evaluate or args.replay_drift or args.verify_calls
    scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
    checklist = load_checklist(args.checklist)
    store = RegistryStore()
    if args.evaluate:
        include = ("validation", "change_request", "approve")
        keys = ("decisions",)
    elif args.replay_drift:
        include = ("validation", "feedback", "change_request", "approve")
        keys = ("decisions", "drift_actions")
    else:
        include = ("validation", "feedback", "change_request", "approve", "call")
        keys = ("decisions", "drift_actions", "change_requests", "call_results")
    report = run_scenario(store, checklist, scenario, include=include)
    print(json.dumps({key: report[key] for key in keys}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--data-file", help="台账快照文件：启动时加载，写操作后保存")
    parser.add_argument("--checklist", help="评估清单路径，默认使用仓库内置清单")
    parser.add_argument("--evaluate", metavar="SCENARIO", help="复算情景文件中的准入结论")
    parser.add_argument("--replay-drift", metavar="SCENARIO", help="回放情景文件中的反馈流")
    parser.add_argument("--verify-calls", metavar="SCENARIO", help="核对情景文件中的线上调用")
    args = parser.parse_args()

    if args.check:
        checklist = load_checklist(args.checklist)
        assert health_payload()["name"] == SERVICE_NAME
        assert checklist["default_rule"]["min_sample_size"] > 0
        print("基础检查通过")
        return

    if args.evaluate or args.replay_drift or args.verify_calls:
        _run_scenario_command(args)
        return

    Handler.checklist = load_checklist(args.checklist)
    if args.data_file and Path(args.data_file).exists():
        Handler.store = RegistryStore.load(args.data_file)
    else:
        Handler.store = RegistryStore()
    Handler.data_file = args.data_file
    print(f"{SERVICE_NAME} 监听 0.0.0.0:{args.port}")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
