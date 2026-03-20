"""
ComfyUI Florence-2 캡셔닝 배치 스크립트
- hari_captioning.json 워크플로우 기반
- upscaled_hari 폴더의 이미지들을 순서대로 처리
- 각 이미지와 동일한 이름의 .txt 파일로 캡션 저장
- 캡션 형식: "hari, {Florence2 detailed caption}"
"""

import json
import random
import requests
import time
import os
import sys
import websocket  # pip install websocket-client

# ========== 설정 ==========
COMFYUI_URL = "https://5exufsihauxeuo-8188.proxy.runpod.net"
INPUT_DIR = r"C:\Workspaces\SKN22-Final-4Team-WEB\generated_images\하리\real_hari\upscaled_hari"
# 캡션 파일 저장 위치 (이미지 파일과 동일한 폴더)
SAVE_DIR = INPUT_DIR
# 캡션 앞에 붙을 트리거 워드
TRIGGER_WORD = "hari, "
# Florence2 모델
FLORENCE2_MODEL = "microsoft/Florence-2-large"
# ==========================


def upload_image(filepath: str, max_retries: int = 3) -> dict:
    """ComfyUI 서버에 이미지 업로드 (대용량 파일 대응, 재시도 포함)"""
    url = f"{COMFYUI_URL}/upload/image"
    filename = os.path.basename(filepath)
    file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
    # 파일 크기에 비례해 타임아웃 설정 (최소 120초, 1MB당 30초 추가)
    upload_timeout = max(120, int(file_size_mb * 30))

    for attempt in range(1, max_retries + 1):
        try:
            with open(filepath, "rb") as f:
                response = requests.post(
                    url,
                    files={"image": (filename, f, "image/png")},
                    data={"type": "input", "overwrite": "true"},
                    timeout=upload_timeout
                )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.Timeout:
            if attempt < max_retries:
                wait = attempt * 10
                print(f"  ⏱️  업로드 타임아웃 (시도 {attempt}/{max_retries}), {wait}초 후 재시도...")
                time.sleep(wait)
            else:
                raise
        except requests.exceptions.RequestException as e:
            if attempt < max_retries:
                wait = attempt * 10
                print(f"  ⚠️  업로드 오류: {e} (시도 {attempt}/{max_retries}), {wait}초 후 재시도...")
                time.sleep(wait)
            else:
                raise


def build_api_prompt(image_name: str) -> dict:
    """
    hari_captioning.json 워크플로우를 API 형식으로 변환
    
    노드 구성:
    - 5 (DownloadAndLoadFlorence2Model) -> florence2_model
    - 9 (LoadImage) -> image
    - 6 (Florence2Run): image + florence2_model -> caption (STRING)
    - 10 (StringConcatenate): "hari, " + caption -> combined string
    - 7 (SaveText|pysssss): combined string 저장 (서버측 저장, 여기선 dummy)
    
    캡션 텍스트는 history에서 Florence2Run 또는 StringConcatenate 출력으로 가져옴
    """
    seed = random.randint(0, 2**53 - 1)

    api_prompt = {
        # Florence2 모델 로드
        "5": {
            "class_type": "DownloadAndLoadFlorence2Model",
            "inputs": {
                "model": FLORENCE2_MODEL,
                "precision": "fp16",
                "attention": "sdpa",
                "load_in_4bit": False
            }
        },
        # 이미지 로드
        "9": {
            "class_type": "LoadImage",
            "inputs": {
                "image": image_name
            }
        },
        # Florence2 실행 - more_detailed_caption
        "6": {
            "class_type": "Florence2Run",
            "inputs": {
                "image": ["9", 0],
                "florence2_model": ["5", 0],
                "text_input": "",
                "task": "more_detailed_caption",
                "fill_mask": True,
                "keep_model_loaded": False,
                "max_new_tokens": 1024,
                "num_beams": 3,
                "do_sample": True,
                "output_mask_select": "",
                "seed": seed
            }
        },
        # "hari, " + caption 연결
        "10": {
            "class_type": "StringConcatenate",
            "inputs": {
                "string_a": TRIGGER_WORD,
                "string_b": ["6", 2],   # Florence2Run의 caption 출력 (slot 2)
                "delimiter": ""
            }
        },
        # SaveText - 서버에도 저장 (dummy 파일명, 실제는 history에서 가져옴)
        "7": {
            "class_type": "ShowText|pysssss",
            "inputs": {
                "text": ["10", 0]
            }
        }
    }
    return api_prompt


def queue_prompt(api_prompt: dict, client_id: str) -> dict:
    """ComfyUI에 프롬프트를 큐에 추가"""
    url = f"{COMFYUI_URL}/prompt"
    payload = {"prompt": api_prompt, "client_id": client_id}
    response = requests.post(url, json=payload, timeout=30)
    if response.status_code != 200:
        try:
            err = response.json()
            print(f"\n  ❌ ComfyUI 에러: {err.get('error', {}).get('type')} - {err.get('error', {}).get('message')}")
            for nid, nerr in err.get('node_errors', {}).items():
                print(f"     node {nid}: {nerr}")
        except Exception:
            print(f"\n  ❌ HTTP {response.status_code}: {response.text[:300]}")
    response.raise_for_status()
    return response.json()


def get_history(prompt_id: str) -> dict:
    """프롬프트 실행 히스토리 조회"""
    url = f"{COMFYUI_URL}/history/{prompt_id}"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def wait_for_completion(ws, prompt_id: str):
    """WebSocket을 통해 프롬프트 실행 완료까지 대기"""
    while True:
        out = ws.recv()
        if isinstance(out, str):
            message = json.loads(out)
            msg_type = message.get("type", "")
            data = message.get("data", {})

            if msg_type == "executing":
                if data.get("prompt_id") == prompt_id:
                    if data.get("node") is None:
                        return  # 완료
                    else:
                        node_id = data.get("node", "")
                        node_names = {
                            "5": "Florence2 모델 로드",
                            "9": "이미지 로드",
                            "6": "캡션 생성(Florence2)",
                            "10": "텍스트 합치기",
                            "7": "결과 출력"
                        }
                        print(f"\r  ⚙️  {node_names.get(node_id, f'node {node_id}')} 실행중...   ", end="")
            elif msg_type == "progress":
                value = data.get("value", 0)
                max_val = data.get("max", 1)
                print(f"\r  진행: {value}/{max_val}   ", end="")
            elif msg_type == "execution_error":
                if data.get("prompt_id") == prompt_id:
                    raise RuntimeError(f"실행 에러: {data.get('exception_message', 'Unknown error')}")


def extract_caption_from_history(history: dict, prompt_id: str) -> str:
    """
    실행 히스토리에서 캡션 텍스트 추출.
    ShowText|pysssss (node 7) 또는 StringConcatenate (node 10) 출력에서 가져옴.
    """
    if prompt_id not in history:
        return None

    outputs = history[prompt_id].get("outputs", {})

    # 우선 node 7 (ShowText) 에서 시도
    if "7" in outputs:
        node_out = outputs["7"]
        # ShowText 출력은 보통 {"text": ["..."]} 형태
        text_list = node_out.get("text", [])
        if text_list:
            return text_list[0] if isinstance(text_list, list) else str(text_list)

    # node 10 (StringConcatenate) 에서 시도
    if "10" in outputs:
        node_out = outputs["10"]
        text_list = node_out.get("text", [])
        if text_list:
            return text_list[0] if isinstance(text_list, list) else str(text_list)

    # node 6 (Florence2Run) caption (slot 2) 에서 시도 후 trigger word 붙이기
    if "6" in outputs:
        node_out = outputs["6"]
        # Florence2Run 출력: image(0), mask(1), caption(2), data(3)
        # API history에서는 'caption' 키로 오거나 인덱스로 접근
        caption = node_out.get("caption") or node_out.get("text", [None])[0]
        if caption:
            return TRIGGER_WORD + str(caption)

    return None


def get_input_images(input_dir: str) -> list:
    """입력 폴더에서 이미지 파일 목록 가져오기 (이미 캡션 파일 있는 것 제외)"""
    supported_ext = {".png", ".jpg", ".jpeg", ".webp"}
    images = []
    for f in sorted(os.listdir(input_dir)):
        ext = os.path.splitext(f)[1].lower()
        if ext in supported_ext:
            images.append(f)
    return images


def main():
    print("=" * 60)
    print("  ComfyUI Florence-2 캡셔닝 배치")
    print("  hari_captioning.json 워크플로우")
    print("=" * 60)

    # 이미지 목록
    input_images = get_input_images(INPUT_DIR)
    if not input_images:
        print(f"\n❌ 입력 폴더에 이미지가 없습니다: {INPUT_DIR}")
        sys.exit(1)

    # 이미 캡션 파일 있는 것 필터링
    pending = []
    skipped = []
    for img in input_images:
        base = os.path.splitext(img)[0]
        txt_path = os.path.join(SAVE_DIR, base + ".txt")
        if os.path.exists(txt_path):
            skipped.append(img)
        else:
            pending.append(img)

    print(f"\n📁 입력 폴더: {INPUT_DIR}")
    print(f"📁 저장 폴더: {SAVE_DIR}")
    print(f"🏷️  트리거 워드: \"{TRIGGER_WORD}\"")
    print(f"🎯 전체 이미지: {len(input_images)}장")
    print(f"   ✅ 이미 완료: {len(skipped)}장 (스킵)")
    print(f"   🔄 처리 예정: {len(pending)}장")
    print(f"🌐 ComfyUI 서버: {COMFYUI_URL}")

    if not pending:
        print("\n✅ 모든 이미지에 캡션이 이미 생성되어 있습니다!")
        return

    # 서버 연결 확인
    try:
        resp = requests.get(f"{COMFYUI_URL}/system_stats", timeout=10)
        resp.raise_for_status()
        print("\n✅ ComfyUI 서버 연결 성공!")
    except Exception as e:
        print(f"\n❌ ComfyUI 서버에 연결할 수 없습니다: {e}")
        sys.exit(1)

    # ShowText|pysssss 노드 존재 여부 확인, 없으면 대안 사용
    use_show_text = True
    try:
        r = requests.get(f"{COMFYUI_URL}/object_info/ShowText|pysssss", timeout=10)
        if r.status_code != 200 or "ShowText|pysssss" not in r.json():
            use_show_text = False
    except Exception:
        use_show_text = False

    if not use_show_text:
        print("⚠️  ShowText|pysssss 노드 없음 → Florence2Run 직접 출력 사용")

    # 클라이언트 ID & WebSocket
    client_id = f"caption_{random.randint(0, 999999):06d}"
    ws_url = COMFYUI_URL.replace("https://", "wss://").replace("http://", "ws://")
    ws = websocket.WebSocket()
    ws.connect(f"{ws_url}/ws?clientId={client_id}")
    print(f"🔌 WebSocket 연결 완료 (client_id: {client_id})")

    print(f"\n{'='*60}")
    print(f"  캡셔닝 시작...")
    print(f"{'='*60}\n")

    successful = 0
    failed = 0

    for i, img_name in enumerate(pending):
        base_name = os.path.splitext(img_name)[0]
        txt_path = os.path.join(SAVE_DIR, base_name + ".txt")

        print(f"[{i+1}/{len(pending)}] 🖼️  {img_name}")

        try:
            # 이미지 업로드
            img_path = os.path.join(INPUT_DIR, img_name)
            upload_result = upload_image(img_path)
            uploaded_name = upload_result.get("name", img_name)
            print(f"  📤 업로드 완료: {uploaded_name}")

            # 프롬프트 구성
            api_prompt = build_api_prompt(uploaded_name)

            # 큐 등록
            result = queue_prompt(api_prompt, client_id)
            prompt_id = result["prompt_id"]
            print(f"  📋 큐 등록 (prompt_id: {prompt_id[:8]}...)")

            # 완료 대기
            wait_for_completion(ws, prompt_id)
            print(f"\r  ✅ 캡션 생성 완료!                        ")

            # 히스토리에서 캡션 추출
            time.sleep(0.5)
            history = get_history(prompt_id)
            caption = extract_caption_from_history(history, prompt_id)

            if caption:
                # 이미지와 동일한 이름으로 .txt 저장
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(caption)
                print(f"  💾 저장: {base_name}.txt")
                print(f"  📝 캡션: {caption[:100]}{'...' if len(caption) > 100 else ''}")
                successful += 1
            else:
                # history에서 못 찾으면 outputs 내용 출력
                outputs = history.get(prompt_id, {}).get("outputs", {})
                print(f"  ⚠️  캡션을 추출하지 못했습니다.")
                print(f"      출력 노드 키: {list(outputs.keys())}")
                for k, v in outputs.items():
                    print(f"      node {k}: {str(v)[:200]}")
                failed += 1

        except Exception as e:
            print(f"\n  ❌ 에러: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

        print()

    ws.close()

    print(f"{'='*60}")
    print(f"  캡셔닝 완료!")
    print(f"  ✅ 성공: {successful}장")
    if failed > 0:
        print(f"  ❌ 실패: {failed}장")
    print(f"  📁 저장 경로: {SAVE_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
