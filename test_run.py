import asyncio
import os
import sys

# Windows console encoding fix
sys.stdout.reconfigure(encoding='utf-8')

# Set required environment variables before importing server
os.environ["CAMSTAR_BASE_URL"] = "https://172.27.251.56/Modeling"
os.environ["CAMSTAR_USERNAME"] = "CamstarAdmin"
os.environ["CAMSTAR_PASSWORD"] = "Cam1star"

import server

async def main():
    print("=== Test 1: get_specs_count ===")
    try:
        count_result = await getattr(server, "get_specs_count")()
        print(count_result)
    except Exception as e:
        print(f"Error: {e}")

    print("\n=== Test 2: list_specs (limited output) ===")
    try:
        list_result = await getattr(server, "list_specs")()
        # Truncate string output to avoid flooding output
        print(list_result[:1500] if len(list_result) > 1500 else list_result)
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
