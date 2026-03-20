import os
import textwrap

def split_script_into_chunks(text, max_chars=15):
    """
    Splits a long script into chunks of approximately max_chars length.
    Splits happen at the last space before the limit.
    """
    # Remove multiple spaces and newlines to clean up the text
    text = " ".join(text.split())
    
    chunks = []
    while len(text) > max_chars:
        # Find the last space within the max_chars limit
        split_idx = text.rfind(' ', 0, max_chars + 1)
        
        if split_idx == -1:
            # No space found, forced split at max_chars
            split_idx = max_chars
        
        chunks.append(text[:split_idx].strip())
        text = text[split_idx:].strip()
    
    if text:
        chunks.append(text)
        
    return chunks

def load_and_preprocess(filepath, max_chars=15):
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        return []
        
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
        
    return split_script_into_chunks(content, max_chars)

if __name__ == "__main__":
    # Sample script for testing
    sample_text = (
        "안녕하세요, 하리에요! 여러분, 10년 전 이세돌 구단과 알파고의 뜨거웠던 대결 기억하시나요? "
        "벌써 10년이 흘러 이세돌 구단과 AI가 역사적인 장소에서 다시 만났습니다!"
    )
    
    print(f"Original: {sample_text}")
    print("-" * 30)
    result = split_script_into_chunks(sample_text, max_chars=15)
    for i, chunk in enumerate(result):
        print(f"Chunk {i+1} ({len(chunk)}자): {chunk}")
