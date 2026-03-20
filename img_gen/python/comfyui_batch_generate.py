"""
ComfyUI 배치 이미지 생성 스크립트
- 하리.json 워크플로우를 사용
- 시드를 랜덤으로 바꿔가며 여러 번 실행
- 생성된 이미지를 시드 번호 파일명으로 저장
"""

import json
import random
import requests
import time
import os
import sys
import io
import struct
from pathlib import Path
from urllib.parse import urljoin
import websocket  # pip install websocket-client

# ========== 설정 ==========
COMFYUI_URL = "https://nl5mivm3tl50xv-8188.proxy.runpod.net"  # RunPod ComfyUI 서버
NUM_IMAGES = 100                         # 생성할 이미지 수
SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_images", "하리")
WORKFLOW_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "하리.json")

# 커스텀 프롬프트 (None이면 워크플로우 JSON의 프롬프트 사용)
CUSTOM_PROMPT = """01. [QUALITY & MASTERING]: 8K ultra-HD, film-grade photorealistic quality, grainless precision. Soft pastel color palette reminiscent of a signature cute yet alluring aesthetic, featuring a slightly overexposed, dreamy high-key cinematic tone. Flawless rendering of micro-details without any digital artifacts ensures a hyper-realistic idol-like visual presentation. 02. [CAMERA & OPTICS]: Shot on a Sony A7R V paired with a Sony FE 50mm f/1.2 GM lens to capture the upper body seamlessly. Shallow depth of field (DOF) with an aperture set precisely to f/1.8 creates a razor-sharp focus on the subject's eyes while maintaining a creamy, ethereal softness around the edges. ISO 100 with a fast shutter speed freezes the subtle micro-expressions, enhanced by the lens's pristine optical texture. 03. [SUBJECT & PHYSIOLOGY]: [SUBJECT]: korean 20yo, 168cm height, perfect 8-head body proportion, extremely small head size, narrow feminine shoulders, voluptuous curves, hyper-voluptuous bust, cinched waist, wide hips, subtle thigh gap, long slender legs, pristine porcelain skin, curvy. She exudes a cute yet captivating idol-like charm, staring directly into the lens. Her facial features are delicately symmetrical with a neutral ivory complexion, strictly devoid of any redness. Fine peach fuzz is imperceptible, and her makeup features a matte velvet base with a glossy layered lip tint, emphasizing her pure aura. 04. [WARDROBE & TEXTILE]: She is wearing a tightly fitted, ribbed cotton scoop-neck crop top in a pure white or soft baby pink, aggressively accentuating her hyper-voluptuous bust and cinched waist. The 100 percent premium cotton fabric stretches visibly across her curves, showing realistic tension folds around the bustline and underarms. The micro-stitching along the neckline is flawlessly rendered, and the fabric's matte weight contrasts beautifully against her pristine porcelain skin. 05. [ENVIRONMENT & ARCHITECTURE]: The background is a seamless, pristine white cyclorama studio wall, completely devoid of distractions. The atmosphere feels airy, pure, and slightly warm, characteristic of a high-end minimalist editorial portrait. The spatial humidity is low, ensuring a crisp, clean separation between the subject's silhouette and the pure white infinity backdrop. 06. [ACTION & POSTURE]: She is positioned in a direct front-view, upper-body framing, staring intensely yet softly straight into the camera lens. Her shoulders are slightly relaxed and pulled back to emphasize her voluptuous curves and narrow feminine frame. Her hands are gently resting near her collarbone, with fingers delicately curled, showcasing precise knuckle articulation and relaxed muscle tension. 07. [PHOTOMETRIC LIGHTING]: Illuminated by a massive diffused octabox positioned front-and-center, acting as the key light to produce a soft, shadowless high-key lighting effect. Color temperature is set to a crisp 5200K daylight, ensuring accurate and pristine ivory skin tones. The shadow fall-off is extremely gentle, almost non-existent on the pure white background, while a subtle catchlight reflects vibrantly in her pupils."""
# ==========================


def load_workflow(filepath: str) -> dict:
    """ComfyUI 워크플로우 JSON 파일 로드"""
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def convert_workflow_to_api_format(workflow: dict) -> dict:
    """
    ComfyUI의 웹 UI 형식 워크플로우를 API 형식으로 변환.
    이 워크플로우는 서브그래프(f2fdebf6...)를 사용하므로,
    /api/prompt에 직접 큐잉할 수 있도록 변환합니다.
    """
    # 이 워크플로우는 서브그래프가 포함된 형식이므로
    # ComfyUI의 /prompt 엔드포인트에 직접 전송하기 위해
    # 서브그래프 내부 노드들을 풀어서 평탄화합니다.
    
    api_prompt = {}
    
    # 서브그래프 정의에서 내부 노드들을 추출
    subgraph_def = workflow["definitions"]["subgraphs"][0]
    inner_nodes = subgraph_def["nodes"]
    
    # 외부 노드 57의 위젯 값 (prompt text, width, height, steps, seed, ...)
    outer_node_57 = None
    save_image_node = None
    for node in workflow["nodes"]:
        if node["id"] == 57:
            outer_node_57 = node
        elif node["id"] == 9:
            save_image_node = node

    widget_values_57 = outer_node_57["widgets_values"]
    # widget_values_57 순서: [text, width, height, steps, seed, control_after_generate, unet_name, clip_name, vae_name]
    prompt_text = widget_values_57[0]
    # 커스텀 프롬프트가 있으면 오버라이드
    if CUSTOM_PROMPT:
        prompt_text = CUSTOM_PROMPT
    width = widget_values_57[1]
    height = widget_values_57[2]
    steps = widget_values_57[3]
    # seed는 widget_values_57[4] - 서브그래프 내 KSampler의 seed에 대응
    # control_after_generate는 widget_values_57[5]
    unet_name = widget_values_57[6]
    clip_name = widget_values_57[7]
    vae_name = widget_values_57[8]
    
    # 내부 노드들을 API 형식으로 변환
    # 노드 ID 매핑 (내부 노드 ID -> API 노드 ID)
    # 내부 노드: 30(CLIPLoader), 29(VAELoader), 33(ConditioningZeroOut), 
    #            8(VAEDecode), 28(UNETLoader), 27(CLIPTextEncode), 
    #            13(EmptySD3LatentImage), 11(ModelSamplingAuraFlow), 3(KSampler)
    
    # UNETLoader (node 28)
    api_prompt["28"] = {
        "class_type": "UNETLoader",
        "inputs": {
            "unet_name": unet_name,
            "weight_dtype": "default"
        }
    }
    
    # CLIPLoader (node 30)
    api_prompt["30"] = {
        "class_type": "CLIPLoader",
        "inputs": {
            "clip_name": clip_name,
            "type": "lumina2",
            "device": "default"
        }
    }
    
    # VAELoader (node 29)
    api_prompt["29"] = {
        "class_type": "VAELoader",
        "inputs": {
            "vae_name": vae_name
        }
    }
    
    # CLIPTextEncode (node 27)
    api_prompt["27"] = {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "text": prompt_text,
            "clip": ["30", 0]
        }
    }
    
    # ConditioningZeroOut (node 33)
    api_prompt["33"] = {
        "class_type": "ConditioningZeroOut",
        "inputs": {
            "conditioning": ["27", 0]
        }
    }
    
    # EmptySD3LatentImage (node 13)
    api_prompt["13"] = {
        "class_type": "EmptySD3LatentImage",
        "inputs": {
            "width": width,
            "height": height,
            "batch_size": 1
        }
    }
    
    # ModelSamplingAuraFlow (node 11)
    api_prompt["11"] = {
        "class_type": "ModelSamplingAuraFlow",
        "inputs": {
            "shift": 3,
            "model": ["28", 0]
        }
    }
    
    # KSampler (node 3) - 시드를 여기서 변경
    api_prompt["3"] = {
        "class_type": "KSampler",
        "inputs": {
            "seed": 0,  # 나중에 변경됨
            "steps": steps,
            "cfg": 1,
            "sampler_name": "res_multistep",
            "scheduler": "simple",
            "denoise": 1,
            "model": ["11", 0],
            "positive": ["27", 0],
            "negative": ["33", 0],
            "latent_image": ["13", 0]
        }
    }
    
    # VAEDecode (node 8)
    api_prompt["8"] = {
        "class_type": "VAEDecode",
        "inputs": {
            "samples": ["3", 0],
            "vae": ["29", 0]
        }
    }
    
    # SaveImage (node 9) - prefix를 시드번호로 변경
    api_prompt["9"] = {
        "class_type": "SaveImage",
        "inputs": {
            "filename_prefix": "하리",
            "images": ["8", 0]
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
                    return
        else:
            # 바이너리 데이터 (미리보기 등) - 무시
            pass


def main():
    print("=" * 60)
    print("  ComfyUI 배치 이미지 생성 (하리.json)")
    print("=" * 60)
    
    # 저장 디렉토리 생성
    os.makedirs(SAVE_DIR, exist_ok=True)
    print(f"\n📁 저장 경로: {SAVE_DIR}")
    print(f"🎯 생성 수: {NUM_IMAGES}장")
    print(f"🌐 ComfyUI 서버: {COMFYUI_URL}")
    
    # 워크플로우 로드 및 API 형식 변환
    print(f"\n📄 워크플로우 로드: {WORKFLOW_FILE}")
    workflow = load_workflow(WORKFLOW_FILE)
    api_prompt = convert_workflow_to_api_format(workflow)
    
    # ComfyUI 서버 연결 확인
    try:
        resp = requests.get(f"{COMFYUI_URL}/system_stats", timeout=5)
        resp.raise_for_status()
        print("✅ ComfyUI 서버 연결 성공!")
    except Exception as e:
        print(f"❌ ComfyUI 서버에 연결할 수 없습니다: {e}")
        print(f"   서버 주소를 확인하세요: {COMFYUI_URL}")
        sys.exit(1)
    
    # 클라이언트 ID 생성
    client_id = f"batch_{random.randint(0, 999999):06d}"
    
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
    
    for i in range(NUM_IMAGES):
        # 랜덤 시드 생성 (ComfyUI는 최대 2^53-1까지 지원)
        seed = random.randint(0, 2**53 - 1)
        
        print(f"[{i+1}/{NUM_IMAGES}] 🎲 시드: {seed}")
        
        # 시드 설정
        api_prompt["3"]["inputs"]["seed"] = seed
        
        # 파일명 prefix를 시드 번호로 설정
        api_prompt["9"]["inputs"]["filename_prefix"] = str(seed)
        
        try:
            # 프롬프트 큐에 추가
            result = queue_prompt(api_prompt, client_id)
            prompt_id = result["prompt_id"]
            print(f"  📤 큐 등록 완료 (prompt_id: {prompt_id})")
            
            # 실행 완료 대기
            print(f"  ⏳ 생성 대기중...", end="")
            wait_for_completion(ws, prompt_id)
            print(f"\r  ✅ 생성 완료!        ")
            
            # 히스토리에서 이미지 정보 가져오기
            time.sleep(0.5)  # 약간의 대기
            history = get_history(prompt_id)
            
            if prompt_id in history:
                outputs = history[prompt_id].get("outputs", {})
                # SaveImage 노드(9)의 출력 가져오기
                if "9" in outputs:
                    images = outputs["9"].get("images", [])
                    for img_info in images:
                        filename = img_info["filename"]
                        subfolder = img_info.get("subfolder", "")
                        folder_type = img_info.get("type", "output")
                        
                        # 이미지 다운로드
                        image_data = get_image(filename, subfolder, folder_type)
                        
                        # 시드 번호로 파일 저장
                        save_path = os.path.join(SAVE_DIR, f"{seed}.png")
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
