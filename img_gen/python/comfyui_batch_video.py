"""
ComfyUI 배치 비디오 생성 스크립트
- hari_vid.json 워크플로우 (Wan 2.2 Image-to-Video) 사용
- 하나의 이미지로 여러 프롬프트의 짧은 영상을 생성
- 파일명은 프롬프트 텍스트로 저장
"""

import json
import random
import requests
import time
import os
import sys
import re
from pathlib import Path
# WebSocket 대신 HTTP 폴링 사용 (RunPod 프록시 타임아웃 방지)

# ========== 설정 ==========
COMFYUI_URL = "https://c4z197av3ovxo5-8188.proxy.runpod.net/"  # RunPod ComfyUI 서버
SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_videos", "하리")
WORKFLOW_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hari_vid.json")

# ComfyUI input 폴더에 업로드된 이미지 파일명
INPUT_IMAGE = "3992524894459651.png"

# 비디오 생성용 프롬프트 리스트
PROMPTS = [
    "Open eyes Move the head down to up.",
    "Open eyes Move the head up to down.",
    "Open eyes Move the head left to right.",
    "Open eyes Move the head right to left.",
    "Make a heart with your fingers",
    "She opens her eyes wide and his/her mouth wide.",
    "eyes widen and her mouth drops open in shock.",
    "eyes lower, shimmering with tears, and her lips tremble as she whispers.",
    "eyes sparkle, and her mouth opens in a broad, carefree laugh.",
    "eyes narrow, and her mouth opens to shout with clenched teeth.",
    "eyes dart nervously, and her mouth opens as he gasps for breath.",
    "eyes squint, and her mouth twists open in a sneer.",
    "eyes flicker away, and her mouth opens awkwardly as he stammers.",
    "eyes shine brightly, and her mouth opens in eager anticipation.",
    "eyes freeze wide, and his mouth hangs open, unable to speak.",
    "eyes roll upward, and her mouth opens in a mocking laugh.",
]

# 모델 설정 (워크플로우에서 추출)
HIGH_NOISE_MODEL = "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors"
LOW_NOISE_MODEL = "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"
HIGH_NOISE_LORA = "wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors"
LOW_NOISE_LORA = "wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors"
CLIP_MODEL = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
VAE_MODEL = "wan_2.1_vae.safetensors"

# 비디오 설정
VIDEO_WIDTH = 640
VIDEO_HEIGHT = 640
VIDEO_LENGTH = 81  # 프레임 수
SAMPLER = "euler"
TURBO_MODE = True  # 4steps LoRA 사용
# ==========================


def sanitize_filename(name: str) -> str:
    """파일명에 사용할 수 없는 문자 제거"""
    # 파일명에 사용할 수 없는 문자를 언더스코어로 대체
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', name)
    # 연속된 언더스코어 제거
    sanitized = re.sub(r'_+', '_', sanitized)
    # 앞뒤 공백/언더스코어 제거
    sanitized = sanitized.strip(' _')
    # 파일명 길이 제한 (확장자 포함 200자)
    if len(sanitized) > 195:
        sanitized = sanitized[:195]
    return sanitized


def build_api_prompt(prompt_text: str, seed: int) -> dict:
    """
    Wan 2.2 I2V 워크플로우를 API 형식으로 변환.
    서브그래프 내부 노드들을 풀어서 직접 구성합니다.
    4steps LoRA 터보 모드를 사용하는 구성입니다.
    """
    api_prompt = {}

    # CLIPLoader (node 84) - Text Encoder
    api_prompt["84"] = {
        "class_type": "CLIPLoader",
        "inputs": {
            "clip_name": CLIP_MODEL,
            "type": "wan",
            "device": "default"
        }
    }

    # VAELoader (node 90)
    api_prompt["90"] = {
        "class_type": "VAELoader",
        "inputs": {
            "vae_name": VAE_MODEL
        }
    }

    # UNETLoader - High Noise (node 95)
    api_prompt["95"] = {
        "class_type": "UNETLoader",
        "inputs": {
            "unet_name": HIGH_NOISE_MODEL,
            "weight_dtype": "default"
        }
    }

    # UNETLoader - Low Noise (node 96)
    api_prompt["96"] = {
        "class_type": "UNETLoader",
        "inputs": {
            "unet_name": LOW_NOISE_MODEL,
            "weight_dtype": "default"
        }
    }

    # LoraLoaderModelOnly - High Noise LoRA (node 101)
    api_prompt["101"] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {
            "model": ["95", 0],
            "lora_name": HIGH_NOISE_LORA,
            "strength_model": 1.0
        }
    }

    # LoraLoaderModelOnly - Low Noise LoRA (node 102)
    api_prompt["102"] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {
            "model": ["96", 0],
            "lora_name": LOW_NOISE_LORA,
            "strength_model": 1.0
        }
    }

    # CLIPTextEncode - Positive Prompt (node 93)
    api_prompt["93"] = {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "text": prompt_text,
            "clip": ["84", 0]
        }
    }

    # CLIPTextEncode - Negative Prompt (node 89)
    api_prompt["89"] = {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "text": "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走",
            "clip": ["84", 0]
        }
    }

    # LoadImage (node 97) - 입력 이미지
    api_prompt["97"] = {
        "class_type": "LoadImage",
        "inputs": {
            "image": INPUT_IMAGE
        }
    }

    # WanImageToVideo (node 98) - I2V 조건 생성
    api_prompt["98"] = {
        "class_type": "WanImageToVideo",
        "inputs": {
            "positive": ["93", 0],
            "negative": ["89", 0],
            "vae": ["90", 0],
            "start_image": ["97", 0],
            "width": VIDEO_WIDTH,
            "height": VIDEO_HEIGHT,
            "length": VIDEO_LENGTH,
            "batch_size": 1
        }
    }

    # ModelSamplingSD3 - High Noise (node 104)
    api_prompt["104"] = {
        "class_type": "ModelSamplingSD3",
        "inputs": {
            "shift": 5.0,
            "model": ["101", 0]
        }
    }

    # ModelSamplingSD3 - Low Noise (node 103)
    api_prompt["103"] = {
        "class_type": "ModelSamplingSD3",
        "inputs": {
            "shift": 5.0,
            "model": ["102", 0]
        }
    }

    # KSamplerAdvanced - 1st pass / High Noise (node 86)
    # WanImageToVideo 출력: 0=positive, 1=negative, 2=latent
    api_prompt["86"] = {
        "class_type": "KSamplerAdvanced",
        "inputs": {
            "add_noise": "enable",
            "noise_seed": seed,
            "steps": 4,
            "cfg": 1,
            "sampler_name": SAMPLER,
            "scheduler": "simple",
            "start_at_step": 0,
            "end_at_step": 2,
            "return_with_leftover_noise": "enable",
            "model": ["104", 0],
            "positive": ["98", 0],
            "negative": ["98", 1],
            "latent_image": ["98", 2]
        }
    }

    # KSamplerAdvanced - 2nd pass / Low Noise (node 85)
    api_prompt["85"] = {
        "class_type": "KSamplerAdvanced",
        "inputs": {
            "add_noise": "disable",
            "noise_seed": 0,
            "steps": 4,
            "cfg": 1,
            "sampler_name": SAMPLER,
            "scheduler": "simple",
            "start_at_step": 2,
            "end_at_step": 4,
            "return_with_leftover_noise": "disable",
            "model": ["103", 0],
            "positive": ["98", 0],
            "negative": ["98", 1],
            "latent_image": ["86", 0]
        }
    }

    # VAEDecode (node 87) - Latent → Image
    api_prompt["87"] = {
        "class_type": "VAEDecode",
        "inputs": {
            "samples": ["85", 0],
            "vae": ["90", 0]
        }
    }

    # CreateVideo (node 94) - Image → Video
    api_prompt["94"] = {
        "class_type": "CreateVideo",
        "inputs": {
            "images": ["87", 0],
            "fps": 16.0
        }
    }

    # SaveVideo (node 108) - 비디오 저장
    api_prompt["108"] = {
        "class_type": "SaveVideo",
        "inputs": {
            "video": ["94", 0],
            "filename_prefix": sanitize_filename(prompt_text),
            "format": "auto",
            "codec": "auto"
        }
    }

    return api_prompt


def upload_image(image_path: str) -> str:
    """ComfyUI에 이미지 업로드"""
    url = f"{COMFYUI_URL}/upload/image"
    filename = os.path.basename(image_path)

    with open(image_path, "rb") as f:
        files = {"image": (filename, f, "image/png")}
        response = requests.post(url, files=files)
        response.raise_for_status()

    result = response.json()
    print(f"  📤 이미지 업로드 완료: {result.get('name', filename)}")
    return result.get("name", filename)


def queue_prompt(api_prompt: dict, client_id: str) -> dict:
    """ComfyUI에 프롬프트를 큐에 추가"""
    url = f"{COMFYUI_URL}/prompt"
    payload = {
        "prompt": api_prompt,
        "client_id": client_id
    }
    response = requests.post(url, json=payload)
    if response.status_code != 200:
        print(f"  ⚠️ 서버 응답 ({response.status_code}): {response.text[:500]}")
    response.raise_for_status()
    return response.json()


def get_history(prompt_id: str) -> dict:
    """프롬프트 실행 히스토리 조회"""
    url = f"{COMFYUI_URL}/history/{prompt_id}"
    response = requests.get(url)
    response.raise_for_status()
    return response.json()


def get_video(filename: str, subfolder: str, folder_type: str) -> bytes:
    """ComfyUI에서 생성된 비디오 다운로드"""
    url = f"{COMFYUI_URL}/view"
    params = {
        "filename": filename,
        "subfolder": subfolder,
        "type": folder_type
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.content


def wait_for_completion_polling(prompt_id: str, poll_interval: int = 5, timeout: int = 1800):
    """HTTP 폴링으로 프롬프트 실행 완료까지 대기 (RunPod 프록시 타임아웃 방지)"""
    start_time = time.time()
    last_status = ""
    
    while True:
        elapsed = time.time() - start_time
        if elapsed > timeout:
            print(f"\n  ⏰ 타임아웃 ({timeout}초 초과)")
            return False
        
        try:
            # 큐 상태 확인
            queue_resp = requests.get(f"{COMFYUI_URL}/queue", timeout=10)
            queue_data = queue_resp.json()
            running = queue_data.get("queue_running", [])
            pending = queue_data.get("queue_pending", [])
            
            # 현재 프롬프트가 실행 중인지 확인
            is_running = any(item[1] == prompt_id for item in running)
            is_pending = any(item[1] == prompt_id for item in pending)
            
            status_msg = f"  ⏳ 대기중... ({int(elapsed)}초 경과)"
            if is_running:
                status_msg = f"  🔄 생성중... ({int(elapsed)}초 경과)"
            elif is_pending:
                status_msg = f"  📋 큐 대기중... ({int(elapsed)}초 경과)"
            
            if status_msg != last_status:
                print(f"\r{status_msg}          ", end="")
                last_status = status_msg
            else:
                print(f"\r{status_msg}          ", end="")
            
            # 히스토리에서 완료 확인
            if not is_running and not is_pending:
                hist_resp = requests.get(f"{COMFYUI_URL}/history/{prompt_id}", timeout=10)
                hist_data = hist_resp.json()
                if prompt_id in hist_data:
                    status = hist_data[prompt_id].get("status", {})
                    if status.get("completed", False):
                        print(f"\r  ✅ 생성 완료! ({int(elapsed)}초 소요)          ")
                        return True
                    elif status.get("status_str") == "error":
                        msgs = status.get("messages", [])
                        print(f"\n  ❌ 실행 에러: {msgs}")
                        return False
                    else:
                        # 완료됨 (status 필드가 없는 경우도 있음)
                        print(f"\r  ✅ 생성 완료! ({int(elapsed)}초 소요)          ")
                        return True
                        
        except requests.exceptions.RequestException as e:
            print(f"\r  ⚠️ 폴링 에러 (재시도): {e}          ", end="")
        
        time.sleep(poll_interval)


def main():
    print("=" * 60)
    print("  ComfyUI 배치 비디오 생성 (Wan 2.2 I2V)")
    print("=" * 60)

    # 저장 디렉토리 생성
    os.makedirs(SAVE_DIR, exist_ok=True)
    print(f"\n📁 저장 경로: {SAVE_DIR}")
    print(f"🎯 생성 수: {len(PROMPTS)}개 영상")
    print(f"🌐 ComfyUI 서버: {COMFYUI_URL}")
    print(f"🖼️  입력 이미지: {INPUT_IMAGE}")

    # ComfyUI 서버 연결 확인
    try:
        resp = requests.get(f"{COMFYUI_URL}/system_stats", timeout=10)
        resp.raise_for_status()
        print("✅ ComfyUI 서버 연결 성공!")
    except Exception as e:
        print(f"❌ ComfyUI 서버에 연결할 수 없습니다: {e}")
        print(f"   서버 주소를 확인하세요: {COMFYUI_URL}")
        sys.exit(1)

    # 입력 이미지 업로드 (ComfyUI input 폴더에 아직 없을 수 있으므로)
    image_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_images", "하리", INPUT_IMAGE)
    if os.path.exists(image_path):
        print(f"\n📤 입력 이미지 업로드 중...")
        uploaded_name = upload_image(image_path)
    else:
        print(f"\n⚠️ 로컬 이미지 파일을 찾을 수 없습니다: {image_path}")
        print(f"   ComfyUI input 폴더에 {INPUT_IMAGE}가 이미 있다고 가정합니다.")

    # 클라이언트 ID 생성
    client_id = f"video_batch_{random.randint(0, 999999):06d}"
    print(f"🆔 클라이언트 ID: {client_id}")
    print(f"📡 HTTP 폴링 모드 (RunPod 프록시 안정성 보장)")

    print(f"\n{'='*60}")
    print(f"  비디오 생성 시작...")
    print(f"{'='*60}\n")

    successful = 0
    failed = 0

    for i, prompt in enumerate(PROMPTS):
        seed = random.randint(0, 2**53 - 1)
        safe_name = sanitize_filename(prompt)

        print(f"[{i+1}/{len(PROMPTS)}] 🎬 프롬프트: {prompt}")
        print(f"  🎲 시드: {seed}")

        # API 프롬프트 구성
        api_prompt = build_api_prompt(prompt, seed)

        try:
            # 프롬프트 큐에 추가
            result = queue_prompt(api_prompt, client_id)
            prompt_id = result["prompt_id"]
            print(f"  📤 큐 등록 완료 (prompt_id: {prompt_id})")

            # HTTP 폴링으로 실행 완료 대기
            if not wait_for_completion_polling(prompt_id):
                print(f"  ⚠️ 생성 실패 또는 타임아웃")
                failed += 1
                print()
                continue

            # 히스토리에서 비디오 정보 가져오기
            time.sleep(1)
            history = get_history(prompt_id)

            if prompt_id in history:
                outputs = history[prompt_id].get("outputs", {})
                # SaveVideo 노드(108)의 출력 가져오기
                if "108" in outputs:
                    videos = outputs["108"].get("videos", [])
                    if not videos:
                        # gifs 키로도 확인
                        videos = outputs["108"].get("gifs", [])
                    for vid_info in videos:
                        filename = vid_info["filename"]
                        subfolder = vid_info.get("subfolder", "")
                        folder_type = vid_info.get("type", "output")

                        # 비디오 다운로드
                        video_data = get_video(filename, subfolder, folder_type)

                        # 확장자 결정
                        ext = os.path.splitext(filename)[1] if os.path.splitext(filename)[1] else ".mp4"

                        # 프롬프트 이름으로 파일 저장
                        save_path = os.path.join(SAVE_DIR, f"{safe_name}{ext}")
                        with open(save_path, "wb") as f:
                            f.write(video_data)

                        file_size_mb = len(video_data) / (1024 * 1024)
                        print(f"  💾 저장: {safe_name}{ext} ({file_size_mb:.1f} MB)")
                        successful += 1
                else:
                    print(f"  ⚠️ SaveVideo 출력을 찾을 수 없습니다.")
                    print(f"     사용 가능한 출력 키: {list(outputs.keys())}")
                    failed += 1
            else:
                print(f"  ⚠️ 히스토리를 찾을 수 없습니다.")
                failed += 1

        except Exception as e:
            print(f"  ❌ 에러 발생: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

        print()

    print(f"{'='*60}")
    print(f"  배치 비디오 생성 완료!")
    print(f"  ✅ 성공: {successful}개")
    if failed > 0:
        print(f"  ❌ 실패: {failed}개")
    print(f"  📁 저장 경로: {SAVE_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
