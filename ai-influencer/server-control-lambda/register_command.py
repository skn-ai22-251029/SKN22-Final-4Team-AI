#!/usr/bin/env python3
"""
일회성 실행 스크립트: Discord에 /server 슬래시 명령어를 글로벌 등록.

사용법:
    python register_command.py \
        --app-id <APPLICATION_ID> \
        --token  <BOT_TOKEN>
"""
import argparse
import json
import sys
import urllib.request

COMMAND_PAYLOAD = {
    "name": "server",
    "description": "EC2 서버를 제어합니다.",
    "options": [
        {
            "type": 1,  # SUB_COMMAND
            "name": "on",
            "description": "EC2 인스턴스를 시작합니다.",
        },
        {
            "type": 1,
            "name": "off",
            "description": "EC2 인스턴스를 종료합니다.",
        },
        {
            "type": 1,
            "name": "status",
            "description": "EC2 인스턴스의 현재 상태를 확인합니다.",
        },
    ],
}


def register(app_id: str, token: str) -> None:
    url = f"https://discord.com/api/v10/applications/{app_id}/commands"
    data = json.dumps(COMMAND_PAYLOAD).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read())
        print(f"등록 완료: /{body['name']} (id={body['id']})")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} {e.reason}")
        print(e.read().decode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-id", required=True, help="Discord Application ID")
    parser.add_argument("--token", required=True, help="Discord Bot Token")
    args = parser.parse_args()
    register(args.app_id, args.token)
