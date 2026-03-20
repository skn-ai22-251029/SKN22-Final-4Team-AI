"""
ComfyUI Klein 옷 갈아입히기 배치 스크립트
- Flux.2 Klein 4B Distilled 워크플로우 사용
- klein_img 폴더의 사진들에 MSE4KG2501BK_M.jpg 옷을 입힘
- reference_image1 = 인물 사진 (변경), reference_image2 = 옷 사진 (고정)
"""

import json
import random
import requests
import time
import os
import sys
import base64
from pathlib import Path
import websocket  # pip install websocket-client

# ========== 설정 ==========
COMFYUI_URL = "https://c4z197av3ovxo5-8188.proxy.runpod.net"  # RunPod ComfyUI 서버
SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_images", "klein_clothing")
INPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "klein_img")
CLOTHING_IMAGE = "MSE4KG2501BK_M.jpg"  # ComfyUI input 폴더에 업로드될 옷 사진 파일명

# 프롬프트: 옷만 교체하도록 지시
PROMPT_TEXT = "Change the clothing of the person to the black jacket provided in the reference image. Keep the person's face, pose, expression, body position, and background exactly the same. Only modify the clothing."

# 모델 설정
UNET_NAME = "flux-2-klein-4b-fp8.safetensors"
CLIP_NAME = "qwen_3_4b.safetensors"
VAE_NAME = "flux2-vae.safetensors"
# ==========================


def upload_image(filepath: str, subfolder: str = "", image_type: str = "input", overwrite: bool = True) -> dict:
    """ComfyUI 서버에 이미지 업로드"""
    url = f"{COMFYUI_URL}/upload/image"
    
    filename = os.path.basename(filepath)
    
    with open(filepath, "rb") as f:
        files = {
            "image": (filename, f, "image/png" if filename.endswith(".png") else "image/jpeg")
        }
        data = {
            "type": image_type,
            "overwrite": str(overwrite).lower()
        }
        if subfolder:
            data["subfolder"] = subfolder
        
        response = requests.post(url, files=files, data=data)
        response.raise_for_status()
        return response.json()


def build_api_prompt(person_image_name: str, clothing_image_name: str, prompt_text: str, seed: int = None) -> dict:
    """
    서브그래프를 풀어서 API 형식 prompt 구성.
    
    워크플로우 구조 (Multiple input - 서브그래프 65c22b29):
    - Node 76 (LoadImage): reference_image1 (인물 사진)
    - Node 81 (LoadImage): reference_image2 (옷 사진)  
    - 서브그래프 내부:
      - 107 (UNETLoader) -> 103 (CFGGuider)
      - 108 (CLIPLoader) -> 109 (CLIPTextEncode)
      - 110 (VAELoader) -> 105 (VAEDecode), 112/84 (Reference Conditioning)
      - 111 (ImageScaleToTotalPixels): reference_image1 스케일
      - 85 (ImageScaleToTotalPixels): reference_image2 스케일 
      - 114 (GetImageSize) -> 102 (Flux2Scheduler), 113 (EmptyFlux2LatentImage)
      - 109 (CLIPTextEncode) -> 86 (ConditioningZeroOut)
      - 112 (Reference Conditioning subgraph): ref_image1 conditioning
      - 84 (Reference Conditioning subgraph): ref_image2 conditioning
      - 103 (CFGGuider) -> 104 (SamplerCustomAdvanced)
      - 106 (RandomNoise) -> 104
      - 101 (KSamplerSelect) -> 104
      - 104 -> 105 (VAEDecode) -> output
    """
    if seed is None:
        seed = random.randint(0, 2**53 - 1)
    
    api_prompt = {}
    
    # ===== 외부 노드 =====
    
    # LoadImage - 인물 사진 (reference_image1)
    api_prompt["76"] = {
        "class_type": "LoadImage",
        "inputs": {
            "image": person_image_name
        }
    }
    
    # LoadImage - 옷 사진 (reference_image2) 
    api_prompt["81"] = {
        "class_type": "LoadImage",
        "inputs": {
            "image": clothing_image_name
        }
    }
    
    # ===== 서브그래프 내부 노드들 (65c22b29) =====
    
    # UNETLoader (107)
    api_prompt["107"] = {
        "class_type": "UNETLoader",
        "inputs": {
            "unet_name": UNET_NAME,
            "weight_dtype": "default"
        }
    }
    
    # CLIPLoader (108)
    api_prompt["108"] = {
        "class_type": "CLIPLoader",
        "inputs": {
            "clip_name": CLIP_NAME,
            "type": "flux2",
            "device": "default"
        }
    }
    
    # VAELoader (110)
    api_prompt["110"] = {
        "class_type": "VAELoader",
        "inputs": {
            "vae_name": VAE_NAME
        }
    }
    
    # CLIPTextEncode - Positive Prompt (109)
    api_prompt["109"] = {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "text": prompt_text,
            "clip": ["108", 0]
        }
    }
    
    # ConditioningZeroOut (86) - negative conditioning
    api_prompt["86"] = {
        "class_type": "ConditioningZeroOut",
        "inputs": {
            "conditioning": ["109", 0]
        }
    }
    
    # ImageScaleToTotalPixels (111) - reference_image1 스케일
    api_prompt["111"] = {
        "class_type": "ImageScaleToTotalPixels",
        "inputs": {
            "upscale_method": "nearest-exact",
            "megapixels": 1,
            "resolution_steps": 64,
            "image": ["76", 0]
        }
    }
    
    # ImageScaleToTotalPixels (85) - reference_image2 스케일
    api_prompt["85"] = {
        "class_type": "ImageScaleToTotalPixels",
        "inputs": {
            "upscale_method": "nearest-exact",
            "megapixels": 1,
            "resolution_steps": 64,
            "image": ["81", 0]
        }
    }
    
    # GetImageSize (114) - reference_image1 크기 구하기
    api_prompt["114"] = {
        "class_type": "GetImageSize",
        "inputs": {
            "image": ["111", 0]
        }
    }
    
    # ===== Reference Conditioning 서브그래프 1 (112 -> 27eacb9f) =====
    # 내부 노드: 116(VAEEncode), 117(ReferenceLatent-positive), 115(ReferenceLatent-negative)
    # ref_image1 conditioning
    
    # VAEEncode (116) - ref_image1을 latent로 인코딩
    api_prompt["116"] = {
        "class_type": "VAEEncode",
        "inputs": {
            "pixels": ["111", 0],
            "vae": ["110", 0]
        }
    }
    
    # ReferenceLatent (117) - positive conditioning with ref_image1
    api_prompt["117"] = {
        "class_type": "ReferenceLatent",
        "inputs": {
            "conditioning": ["109", 0],
            "latent": ["116", 0]
        }
    }
    
    # ReferenceLatent (115) - negative conditioning with ref_image1
    api_prompt["115"] = {
        "class_type": "ReferenceLatent",
        "inputs": {
            "conditioning": ["86", 0],
            "latent": ["116", 0]
        }
    }
    
    # ===== Reference Conditioning 서브그래프 2 (84 -> 93041a64) =====
    # 내부 노드: 119(VAEEncode), 120(ReferenceLatent-positive), 118(ReferenceLatent-negative)
    # ref_image2 conditioning
    
    # VAEEncode (119) - ref_image2를 latent로 인코딩
    api_prompt["119"] = {
        "class_type": "VAEEncode",
        "inputs": {
            "pixels": ["85", 0],
            "vae": ["110", 0]
        }
    }
    
    # ReferenceLatent (120) - positive conditioning with ref_image2
    api_prompt["120"] = {
        "class_type": "ReferenceLatent",
        "inputs": {
            "conditioning": ["117", 0],
            "latent": ["119", 0]
        }
    }
    
    # ReferenceLatent (118) - negative conditioning with ref_image2
    api_prompt["118"] = {
        "class_type": "ReferenceLatent",
        "inputs": {
            "conditioning": ["115", 0],
            "latent": ["119", 0]
        }
    }
    
    # ===== Sampler 관련 =====
    
    # Flux2Scheduler (102)
    api_prompt["102"] = {
        "class_type": "Flux2Scheduler",
        "inputs": {
            "steps": 4,
            "width": ["114", 0],
            "height": ["114", 1]
        }
    }
    
    # EmptyFlux2LatentImage (113)
    api_prompt["113"] = {
        "class_type": "EmptyFlux2LatentImage",
        "inputs": {
            "width": ["114", 0],
            "height": ["114", 1],
            "batch_size": 1
        }
    }
    
    # RandomNoise (106)
    api_prompt["106"] = {
        "class_type": "RandomNoise",
        "inputs": {
            "noise_seed": seed
        }
    }
    
    # KSamplerSelect (101)
    api_prompt["101"] = {
        "class_type": "KSamplerSelect",
        "inputs": {
            "sampler_name": "euler"
        }
    }
    
    # CFGGuider (103)
    api_prompt["103"] = {
        "class_type": "CFGGuider",
        "inputs": {
            "cfg": 1,
            "model": ["107", 0],
            "positive": ["120", 0],
            "negative": ["118", 0]
        }
    }
    
    # SamplerCustomAdvanced (104)
    api_prompt["104"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["106", 0],
            "guider": ["103", 0],
            "sampler": ["101", 0],
            "sigmas": ["102", 0],
            "latent_image": ["113", 0]
        }
    }
    
    # VAEDecode (105)
    api_prompt["105"] = {
        "class_type": "VAEDecode",
        "inputs": {
            "samples": ["104", 0],
            "vae": ["110", 0]
        }
    }
    
    # SaveImage (94)
    api_prompt["94"] = {
        "class_type": "SaveImage",
        "inputs": {
            "filename_prefix": "Flux2-Klein",
            "images": ["105", 0]
        }
    }
    
    return api_prompt


def queue_prompt(api_prompt: dict, client_id: str) -> dict:
    """ComfyUI에 프롬프트를 큐에 추가"""
    url = f"{COMFYUI_URL}/prompt"
    payload = {
        "prompt": api_prompt,
        "client_id": client_id
    }
    response = requests.post(url, json=payload)
    if response.status_code != 200:
        try:
            err_data = response.json()
            err_type = err_data.get('error', {}).get('type', 'unknown')
            err_msg = err_data.get('error', {}).get('message', '')
            node_errors = err_data.get('node_errors', {})
            print(f"\n  ❌ ComfyUI 에러: [{err_type}] {err_msg}")
            for nid, nerr in node_errors.items():
                print(f"     node {nid}: {nerr}")
        except Exception:
            print(f"\n  ❌ HTTP {response.status_code}: {response.text[:300]}")
    response.raise_for_status()
    return response.json()


def get_image(filename: str, subfolder: str, folder_type: str) -> bytes:
    """ComfyUI에서 생성된 이미지 다운로드"""
    url = f"{COMFYUI_URL}/view"
    params = {
        "filename": filename,
        "subfolder": subfolder,
        "type": folder_type
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.content


def get_history(prompt_id: str) -> dict:
    """프롬프트 실행 히스토리 조회"""
    url = f"{COMFYUI_URL}/history/{prompt_id}"
    response = requests.get(url)
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
                        # 실행 완료
                        return
            elif msg_type == "progress":
                value = data.get("value", 0)
                max_val = data.get("max", 1)
                print(f"  진행: {value}/{max_val}", end="\r")
            elif msg_type == "execution_error":
                if data.get("prompt_id") == prompt_id:
                    print(f"\n  ❌ 실행 에러: {data.get('exception_message', 'Unknown error')}")
                    raise RuntimeError(f"Execution error: {data.get('exception_message', 'Unknown error')}")
        else:
            # 바이너리 데이터 (미리보기 등) - 무시
            pass


def get_input_images(input_dir: str) -> list:
    """입력 폴더에서 이미지 파일 목록 가져오기"""
    supported_ext = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    images = []
    for f in os.listdir(input_dir):
        ext = os.path.splitext(f)[1].lower()
        if ext in supported_ext:
            images.append(f)
    images.sort()
    return images


def main():
    print("=" * 60)
    print("  ComfyUI Klein 옷 갈아입히기 배치 생성")
    print("  Flux.2 Klein 4B Distilled - Image Edit")
    print("=" * 60)
    
    # 저장 디렉토리 생성
    os.makedirs(SAVE_DIR, exist_ok=True)
    
    # 입력 이미지 목록
    input_images = get_input_images(INPUT_DIR)
    if not input_images:
        print(f"\n❌ 입력 폴더에 이미지가 없습니다: {INPUT_DIR}")
        sys.exit(1)
    
    print(f"\n📁 입력 폴더: {INPUT_DIR}")
    print(f"📁 저장 경로: {SAVE_DIR}")
    print(f"👔 옷 이미지: {CLOTHING_IMAGE}")
    print(f"🎯 처리할 이미지: {len(input_images)}장")
    print(f"🌐 ComfyUI 서버: {COMFYUI_URL}")
    
    print(f"\n📋 입력 이미지 목록:")
    for idx, img in enumerate(input_images, 1):
        print(f"  {idx}. {img}")
    
    # ComfyUI 서버 연결 확인
    try:
        resp = requests.get(f"{COMFYUI_URL}/system_stats", timeout=10)
        resp.raise_for_status()
        print("\n✅ ComfyUI 서버 연결 성공!")
    except Exception as e:
        print(f"\n❌ ComfyUI 서버에 연결할 수 없습니다: {e}")
        print(f"   서버 주소를 확인하세요: {COMFYUI_URL}")
        sys.exit(1)
    
    # 옷 이미지 업로드
    clothing_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), CLOTHING_IMAGE)
    if not os.path.exists(clothing_path):
        print(f"\n❌ 옷 이미지를 찾을 수 없습니다: {clothing_path}")
        sys.exit(1)
    
    print(f"\n📤 옷 이미지 업로드: {CLOTHING_IMAGE}")
    upload_result = upload_image(clothing_path)
    clothing_uploaded_name = upload_result.get("name", CLOTHING_IMAGE)
    print(f"  ✅ 업로드 완료: {clothing_uploaded_name}")
    
    # 클라이언트 ID 생성
    client_id = f"klein_{random.randint(0, 999999):06d}"
    
    # WebSocket 연결
    ws_url = COMFYUI_URL.replace("http://", "ws://").replace("https://", "wss://")
    ws = websocket.WebSocket()
    ws.connect(f"{ws_url}/ws?clientId={client_id}")
    print(f"🔌 WebSocket 연결 완료 (client_id: {client_id})")
    
    print(f"\n{'='*60}")
    print(f"  이미지 생성 시작...")
    print(f"{'='*60}\n")
    
    successful = 0
    failed = 0
    
    for i, img_name in enumerate(input_images):
        img_path = os.path.join(INPUT_DIR, img_name)
        base_name = os.path.splitext(img_name)[0]
        
        print(f"[{i+1}/{len(input_images)}] 📸 처리중: {img_name}")
        
        try:
            # 인물 이미지 업로드
            print(f"  📤 인물 이미지 업로드...")
            upload_result = upload_image(img_path)
            person_uploaded_name = upload_result.get("name", img_name)
            print(f"  ✅ 업로드 완료: {person_uploaded_name}")
            
            # 랜덤 시드 생성
            seed = random.randint(0, 2**53 - 1)
            print(f"  🎲 시드: {seed}")
            
            # API prompt 구성
            api_prompt = build_api_prompt(
                person_image_name=person_uploaded_name,
                clothing_image_name=clothing_uploaded_name,
                prompt_text=PROMPT_TEXT,
                seed=seed
            )
            
            # 파일명 prefix 설정
            api_prompt["94"]["inputs"]["filename_prefix"] = f"Klein_{base_name}"
            
            # 프롬프트 큐에 추가
            result = queue_prompt(api_prompt, client_id)
            prompt_id = result["prompt_id"]
            print(f"  📤 큐 등록 완료 (prompt_id: {prompt_id})")
            
            # 실행 완료 대기
            print(f"  ⏳ 생성 대기중...", end="")
            wait_for_completion(ws, prompt_id)
            print(f"\r  ✅ 생성 완료!        ")
            
            # 히스토리에서 이미지 정보 가져오기
            time.sleep(0.5)
            history = get_history(prompt_id)
            
            if prompt_id in history:
                outputs = history[prompt_id].get("outputs", {})
                # SaveImage 노드(94)의 출력 가져오기
                if "94" in outputs:
                    images = outputs["94"].get("images", [])
                    for img_info in images:
                        filename = img_info["filename"]
                        subfolder = img_info.get("subfolder", "")
                        folder_type = img_info.get("type", "output")
                        
                        # 이미지 다운로드
                        image_data = get_image(filename, subfolder, folder_type)
                        
                        # 파일 저장
                        save_ext = os.path.splitext(filename)[1] or ".png"
                        save_path = os.path.join(SAVE_DIR, f"{base_name}_clothing{save_ext}")
                        with open(save_path, "wb") as f:
                            f.write(image_data)
                        
                        file_size_kb = len(image_data) / 1024
                        print(f"  💾 저장: {save_path} ({file_size_kb:.1f} KB)")
                        successful += 1
                else:
                    print(f"  ⚠️ SaveImage 출력을 찾을 수 없습니다.")
                    failed += 1
            else:
                print(f"  ⚠️ 히스토리를 찾을 수 없습니다.")
                failed += 1
                
        except Exception as e:
            print(f"  ❌ 에러 발생: {e}")
            failed += 1
        
        print()
    
    # 정리
    ws.close()
    
    print(f"{'='*60}")
    print(f"  배치 생성 완료!")
    print(f"  ✅ 성공: {successful}장")
    if failed > 0:
        print(f"  ❌ 실패: {failed}장")
    print(f"  📁 저장 경로: {SAVE_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
