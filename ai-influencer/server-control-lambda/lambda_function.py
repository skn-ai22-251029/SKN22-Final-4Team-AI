import hashlib
import json
import os
import boto3

DISCORD_PUBLIC_KEY = os.environ["DISCORD_PUBLIC_KEY"]
EC2_INSTANCE_ID = os.environ["EC2_INSTANCE_ID"]
EC2_REGION = os.environ.get("EC2_REGION", "ap-northeast-2")
ALLOWED_USER_IDS = set(
    uid.strip()
    for uid in os.environ.get("DISCORD_ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
)

ec2 = boto3.client("ec2", region_name=EC2_REGION)

# ── Pure Python Ed25519 (표준 라이브러리만 사용) ──────────────────────────
_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q
_Q8 = pow(2, (_Q - 1) // 4, _Q)
_GY = (4 * pow(5, _Q - 2, _Q)) % _Q
_GX = None  # 아래에서 초기화


def _recover_x(y, sign):
    x2 = (y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q) % _Q
    if x2 == 0:
        return 0 if not sign else None
    x = pow(x2, (_Q + 3) // 8, _Q)
    if (x * x - x2) % _Q != 0:
        x = x * _Q8 % _Q
    if (x * x - x2) % _Q != 0:
        return None
    if x & 1 != sign:
        x = _Q - x
    return x


def _point_add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _Q
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _Q
    C = 2 * P[3] * Q[3] * _D % _Q
    D = 2 * P[2] * Q[2] % _Q
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _Q, G * H % _Q, F * G % _Q, E * H % _Q)


def _point_mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _point_add(Q, P)
        P = _point_add(P, P)
        s >>= 1
    return Q


def _point_decompress(s):
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    x = _recover_x(y, s[31] >> 7)
    if x is None:
        raise ValueError("invalid point")
    return (x, y, 1, x * y % _Q)


def _point_equal(P, Q):
    return (P[0] * Q[2] - Q[0] * P[2]) % _Q == 0 and \
           (P[1] * Q[2] - Q[1] * P[2]) % _Q == 0


_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _Q)


def _ed25519_verify(public_key_hex: str, signature_hex: str, message: bytes) -> bool:
    try:
        pk = bytes.fromhex(public_key_hex)
        sig = bytes.fromhex(signature_hex)
        if len(sig) != 64 or len(pk) != 32:
            return False
        A = _point_decompress(pk)
        R = _point_decompress(sig[:32])
        s = int.from_bytes(sig[32:], "little")
        if s >= _L:
            return False
        h = int.from_bytes(
            hashlib.sha512(sig[:32] + pk + message).digest(), "little"
        )
        return _point_equal(_point_mul(s, _G), _point_add(R, _point_mul(h, A)))
    except Exception:
        return False
# ─────────────────────────────────────────────────────────────────────────────


def verify_signature(event: dict) -> bool:
    headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
    signature = headers.get("x-signature-ed25519", "")
    timestamp = headers.get("x-signature-timestamp", "")
    body = event.get("body", "")
    return _ed25519_verify(
        DISCORD_PUBLIC_KEY, signature, f"{timestamp}{body}".encode()
    )


_STATE_KO = {
    "running": "🟢 실행 중",
    "stopped": "🔴 중지됨",
    "stopping": "🟡 중지 중",
    "pending": "🟡 시작 중",
    "shutting-down": "🟡 종료 중",
    "terminated": "⚫ 종료됨",
}


def get_instance_state() -> dict:
    resp = ec2.describe_instances(InstanceIds=[EC2_INSTANCE_ID])
    instance = resp["Reservations"][0]["Instances"][0]
    state_raw = instance["State"]["Name"]
    return {
        "state": _STATE_KO.get(state_raw, state_raw),
        "public_ip": instance.get("PublicIpAddress", "없음 (중지 상태)"),
        "instance_type": instance.get("InstanceType", "N/A"),
    }


def respond(content: str) -> dict:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"type": 4, "data": {"content": content}}),
    }


def handle_server_command(options: list, user_id: str) -> dict:
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return respond("이 명령을 실행할 권한이 없습니다.")

    subcommand = options[0]["name"] if options else None

    if subcommand == "on":
        try:
            info = get_instance_state()
            if info["state"] == "running":
                return respond(f"인스턴스가 이미 실행 중입니다. | IP: {info['public_ip']}")
            ec2.start_instances(InstanceIds=[EC2_INSTANCE_ID])
            return respond("인스턴스 시작 중... 잠시 후 다시 확인해 주세요.")
        except Exception as e:
            return respond(f"시작 실패: {e}")

    elif subcommand == "off":
        try:
            info = get_instance_state()
            if info["state"] == "stopped":
                return respond("인스턴스가 이미 종료된 상태입니다.")
            ec2.stop_instances(InstanceIds=[EC2_INSTANCE_ID])
            return respond("인스턴스 종료 중...")
        except Exception as e:
            return respond(f"종료 실패: {e}")

    elif subcommand == "status":
        try:
            info = get_instance_state()
            return respond(f"상태: {info['state']}\nIP: {info['public_ip']}\n타입: {info['instance_type']}")
        except Exception as e:
            return respond(f"상태 조회 실패: {e}")

    else:
        return respond("알 수 없는 서브커맨드입니다. on / off / status 중 하나를 사용하세요.")


def handler(event, context):
    if not verify_signature(event):
        return {"statusCode": 401, "body": "Invalid request signature"}

    body = json.loads(event.get("body", "{}"))
    interaction_type = body.get("type")

    if interaction_type == 1:  # PING
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"type": 1}),
        }

    if interaction_type == 2:  # APPLICATION_COMMAND
        data = body.get("data", {})
        user_id = (
            body.get("member", {}).get("user", {}).get("id")
            or body.get("user", {}).get("id", "")
        )
        if data.get("name") == "server":
            return handle_server_command(data.get("options", []), user_id)

    return respond("지원하지 않는 인터랙션입니다.")
