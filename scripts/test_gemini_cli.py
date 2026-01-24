import subprocess
import shutil

def test_gemini():
    gemini_path = shutil.which("gemini")
    print(f"Gemini executable found at: {gemini_path}")
    
    if not gemini_path:
        print("❌ 'gemini' command not found in PATH.")
        return

    print("Testing basic invocation...")
    try:
        # 尝试最简单的调用
        prompt = "Hello, say hi back in one word."
        result = subprocess.run(
            [gemini_path, prompt], 
            capture_output=True, 
            text=True, 
            timeout=30
        )
        
        print(f"Return Code: {result.returncode}")
        print(f"Stdout: {result.stdout.strip()}")
        print(f"Stderr: {result.stderr.strip()}")
        
        if result.returncode == 0 and result.stdout:
            print("✅ Success! Gemini CLI works.")
        else:
            print("⚠️ Command failed or produced no output.")

    except Exception as e:
        print(f"❌ Exception running gemini: {e}")

if __name__ == "__main__":
    test_gemini()
