"""Test configuration — adds module directories to sys.path for imports."""

import sys
import os

# Add project subdirectories to path so tests can import modules
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

for subdir in ["api-gateway", "ingestion-worker", "query-engine", "admin-panel"]:
    module_path = os.path.join(project_root, subdir)
    if module_path not in sys.path:
        sys.path.insert(0, module_path)
