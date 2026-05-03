"""
Vercel ASGI Handler - Entry point for @vercel/python runtime
"""
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import and export the FastAPI app
from main import app

# That's it - Vercel's @vercel/python will use this 'app' as the ASGI application

