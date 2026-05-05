import logging
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path.cwd()))

from codebase_agent.tools.memory_tool import MemoryTool

logging.basicConfig(level=logging.INFO)

def test_memory():
    print("Testing MemoryTool...")
    # Mock local config
    base_url = "http://localhost:1234/v1"
    model = "gemma-4-e2b-it"
    
    m = MemoryTool(base_url=base_url, model=model)
    
    if m.memory:
        print("✅ MemoryTool initialized")
        # Try to add a memory (this might fail if the local server is down, but we want to see if it crashes)
        try:
            success = m.add_memory("The codebase uses FastAPI for the API layer.", user_id="test_user")
            print(f"Add memory success: {success}")
            
            results = m.search_memory("What web framework is used?", user_id="test_user")
            print(f"Search results: {results}")
        except Exception as e:
            print(f"❌ Error during memory operations: {e}")
    else:
        print("❌ MemoryTool failed to initialize")

if __name__ == "__main__":
    test_memory()
