"""Supervise the local API/UI; preserve external services and all data on exit."""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
from sqlalchemy import create_engine, text

from ticketmind.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]


class LaunchError(RuntimeError):
    pass


def available(port):
    if not 1 <= port <= 65535:
        raise LaunchError(f"Invalid port: {port}")
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        raise LaunchError(f"Port {port} is unavailable or reserved; choose another --api-port / --ui-port.") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="隔离 PostgreSQL + 合成 Agent，无 Milvus/模型调用")
    parser.add_argument("--postgres-container", action="store_true", help="启动可选 5433 PostgreSQL 容器；需先配置对应 .env")
    parser.add_argument("--postgres-service", help="Windows 已安装的 PostgreSQL 服务名；使用现有数据")
    parser.add_argument("--skip-infra", action="store_true", help="依赖已运行时跳过 Docker 启动")
    parser.add_argument("--api-port", type=int)
    parser.add_argument("--ui-port", type=int, default=8501)
    parser.add_argument("--check", action="store_true", help="只读检查 DB 和端口，不迁移、不启动")
    args = parser.parse_args()
    os.chdir(ROOT)
    api_port = args.api_port or (8010 if args.demo else 8000)
    if api_port == args.ui_port:
        parser.error("API 与 UI 端口必须不同")
    available(api_port)
    available(args.ui_port)
    if not args.check:
        if args.postgres_service:
            if os.name != "nt":
                parser.error("--postgres-service 仅用于 Windows")
            # No shell interpolation of service names.
            subprocess.run(["sc.exe", "start", args.postgres_service], check=False)
        if args.postgres_container:
            subprocess.run(["docker", "compose", "--project-directory", "infra/postgres", "up", "-d", "--wait"], check=True)
        if not args.demo and not args.skip_infra:
            subprocess.run(["docker", "compose", "--project-directory", "infra/milvus", "up", "-d", "--wait"], check=True)
    engine = create_engine(Settings().database_url.unicode_string(), connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()
    if args.check:
        print("PostgreSQL reachable; requested API/UI ports available. No writes performed.")
        return
    if not args.demo:
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    api_args = [sys.executable, "scripts/serve_m5_demo.py", "--port", str(api_port)] if args.demo else [
        sys.executable, "-m", "uvicorn", "ticketmind.main:app", "--host", "127.0.0.1", "--port", str(api_port), "--workers", "1"]
    children = []
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        children.append(subprocess.Popen(api_args, creationflags=flags))
        with httpx.Client(trust_env=False, timeout=2) as client:
            for _ in range(60):
                if children[0].poll() is not None:
                    raise RuntimeError("API startup failed; see log above")
                try:
                    if client.get(f"http://127.0.0.1:{api_port}/health").is_success:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("API startup timeout")
        env = {**os.environ, "TICKETMIND_API_URL": f"http://127.0.0.1:{api_port}"}
        children.append(subprocess.Popen([sys.executable, "-m", "streamlit", "run", "src/ticketmind/workbench/app.py",
            "--server.port", str(args.ui_port), "--server.address", "127.0.0.1", "--server.headless", "true"], env=env, creationflags=flags))
        print(f"Workbench: http://127.0.0.1:{args.ui_port} ; Ctrl+C stops API/UI, preserves database/Milvus data.", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                if os.name == "nt":
                    import signal
                    child.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not echo DB URLs or credentials from third-party exception messages.
        detail = str(exc) if isinstance(exc, LaunchError) else f"{type(exc).__name__}. Check configuration, service/port availability and preceding logs."
        print(f"Startup failed: {detail}", file=sys.stderr)
        raise SystemExit(1)
