"""
Vercel ASGI Handler - Entry point for Vercel's Python runtime
"""
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import the FastAPI app
from main import app

# CRITICAL: Export as handler for Vercel Python runtime
handler = app

