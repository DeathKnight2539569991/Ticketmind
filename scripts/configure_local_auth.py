"""Create missing local demo credentials without printing or replacing existing secrets."""
import secrets
from pathlib import Path

from dotenv import dotenv_values, set_key

from ticketmind.core.config import AuthSettings


def main():
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.exists():
        raise RuntimeError("请先从 env.example 创建 .env 并配置数据库和模型")
    values = dotenv_values(path)
    added = []
    for name in ("TICKETMIND_OPERATOR_TOKEN", "TICKETMIND_REVIEWER_TOKEN"):
        if not values.get(name):
            set_key(str(path), name, secrets.token_urlsafe(32), quote_mode="never")
            added.append(name)
    AuthSettings(_env_file=path)
    print("本地身份配置已验证；新增项：" + (", ".join(added) if added else "无，保留已有配置"))
    print("凭据仅保存在已被 Git 忽略的 .env；未输出凭据值。")


if __name__ == "__main__":
    main()
