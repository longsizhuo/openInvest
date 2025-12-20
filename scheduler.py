import time
import subprocess
import sys
import os
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Fallback for older python versions if needed, though project requires >=3.13
    from datetime import timezone
    ZoneInfo = lambda x: timezone(timedelta(hours=8)) if x == "Asia/Shanghai" else None

def get_next_run_time():
    """Calculates the next occurrence of 10:00 AM Beijing Time."""
    # Beijing Time is Asia/Shanghai
    beijing_tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(beijing_tz)
    
    target_time = now.replace(hour=10, minute=0, second=0, microsecond=0)
    
    if now >= target_time:
        # If it's already past 10:00 AM today, schedule for tomorrow
        target_time += timedelta(days=1)
    
    return target_time

def run_job():
    """Executes the main investment agent script."""
    print(f"\n[Scheduler] Starting job at {datetime.now()}...")
    try:
        # Use the same python interpreter that is running this scheduler
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
        
        # We use subprocess to run the script as a separate process
        # This ensures main.py starts with a fresh state each time
        result = subprocess.run([sys.executable, script_path], check=True, text=True)
        print(f"[Scheduler] Job finished successfully with exit code {result.returncode}.")
    except subprocess.CalledProcessError as e:
        print(f"[Scheduler] Job failed with error: {e}")
    except Exception as e:
        print(f"[Scheduler] An unexpected error occurred: {e}")

def main():
    print("🕒 Investment Agent Scheduler Started")
    print("-------------------------------------")
    print(f"Target Time: 10:00 AM Beijing Time (Daily)")
    print(f"Script: {os.path.join(os.path.dirname(os.path.abspath(__file__)), 'main.py')}")
    print("-------------------------------------\
")

    while True:
        next_run = get_next_run_time()
        now = datetime.now(next_run.tzinfo)
        
        wait_seconds = (next_run - now).total_seconds()
        
        print(f"Next run scheduled for: {next_run} (in {wait_seconds/3600:.2f} hours)")
        
        # Sleep until the target time
        # We break the sleep into smaller chunks to allow for clean interruption (Ctrl+C)
        # though a simple sleep is fine for a basic script.
        try:
            time.sleep(wait_seconds)
            run_job()
            # Sleep a bit to avoid double-triggering within the same second (though replace handles this)
            time.sleep(60) 
        except KeyboardInterrupt:
            print("\n[Scheduler] Stopped by user.")
            break

if __name__ == "__main__":
    main()
