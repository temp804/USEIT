from fastapi import FastAPI, Query, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, Response, JSONResponse, RedirectResponse
import yt_dlp
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import os
from urllib.parse import urlparse, quote, unquote
import time
import re
import logging
from typing import Optional, Dict
import urllib3
# import 
import uuid
import secrets
# import
from datetime import datetime, timedelta
# from pathlib import Path
# import base64

# Optional: Try to import browser_cookie3 (not available on serverless)
# try:
#     import browser_cookie3
#     HAS_BROWSER_COOKIES = True
# except ImportError:
#     HAS_BROWSER_COOKIES = False

# Disable SSL warnings if needed
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Health check endpoint for Vercel
@app.get("/health")
def health_check():
    """Health check endpoint for monitoring"""
    return {
        "status": "ok",
        "service": "Universal Video Downloader",
        "version": "1.0"
    }

CACHE = {}
DOWNLOAD_DIR = "downloads"
TOKEN_DATA_FILE = "token_data.json"

# Try to create download directory, but don't fail if it doesn't work (serverless)
try:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
except (OSError, PermissionError) as e:
    logger.warning(f"Could not create {DOWNLOAD_DIR}: {str(e)}. Using /tmp instead.")
    DOWNLOAD_DIR = "/tmp/downloads"
    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    except Exception as e:
        logger.warning(f"Could not create /tmp/downloads either: {str(e)}")

# Global token storage
TOKEN_DB: Dict[str, Dict] = {}

# ============================================
# ========== VPN & PROXY CONFIGURATION =======
# ============================================

# YOUR SSTP VPN CONFIGURATION
VPN_CONFIG = {
    "enabled": True,
    "type": "SSTP",
    "server": "public-vpn-202.opengw.net",
    "username": "vpn",
    "password": "vpn",
    "port": 443,
    "description": "SSTP VPN Tunnel for VPN-only websites"
}

# VPN Proxy List (Free VPN services and proxy endpoints)
VPN_PROXY_LIST = [
    # Free proxy services
    "http://proxy.example.com:8080",
    # Note: In production, use free proxy API or VPN services like:
    # - Free-proxy-list.net
    # - Proxy-list.download
    # - vpngate.net VPN tunnels
    None,  # Direct connection (no proxy)
]

# Regular proxy list for fallback
PROXY_LIST = [
    None,  # No proxy (direct)
]

# VPN Mode - Set to True to force VPN usage
VPN_MODE = False
VPN_ENABLED = True  # Can be toggled at runtime

# User VPN Configuration (for SSTP/custom VPN)
USER_VPN_CONFIG = {
    "enabled": True,
    "vpn_server": "public-vpn-202.opengw.net",
    "vpn_type": "SSTP",
    "username": "vpn",
    "password": "vpn",
    "port": 443,
    "proxy_url": None,  # Will be calculated from server
}

def build_vpn_proxy_url():
    """Build proxy URL from VPN credentials"""
    try:
        if not USER_VPN_CONFIG["enabled"] or not USER_VPN_CONFIG["username"]:
            return None
        
        vpn_type = USER_VPN_CONFIG.get("vpn_type", "").lower()
        username = USER_VPN_CONFIG.get("username", "")
        password = USER_VPN_CONFIG.get("password", "")
        server = USER_VPN_CONFIG.get("vpn_server", "")
        port = USER_VPN_CONFIG.get("port", 443)
        
        # For SSTP, create HTTP proxy URL with auth
        if vpn_type == "sstp":
            proxy_url = f"http://{username}:{password}@{server}:{port}"
            logger.info(f"✓ SSTP VPN Proxy configured: {server}:{port}")
            return proxy_url
        
        # For other types, try similar format
        proxy_url = f"http://{username}:{password}@{server}:{port}"
        logger.info(f"✓ VPN Proxy configured: {proxy_url}")
        return proxy_url
    except Exception as e:
        logger.warning(f"Failed to build VPN proxy URL: {e}")
        return None

# Initialize VPN proxy (with error handling)
try:
    USER_VPN_CONFIG["proxy_url"] = build_vpn_proxy_url()
except Exception as e:
    logger.warning(f"Could not initialize VPN proxy: {e}")
    USER_VPN_CONFIG["proxy_url"] = None

logger.info(f"App initialized. VPN enabled: {USER_VPN_CONFIG.get('enabled', False)}")

# ============================================
# ========== TOKEN SYSTEM (Unlimited) ==========
# ============================================

def fetch_free_vpn_proxies():
    """Fetch free VPN proxies from public APIs"""
    global VPN_PROXY_LIST
    try:
        logger.info("Fetching free VPN proxies...")
        # Try to get free proxies from free-proxy-list
        response = requests.get('https://www.proxy-list.download/api/v1/get?type=http', timeout=10)
        if response.status_code == 200:
            data = response.json()
            proxies = data.get('LISTA', [])[:5]  # Get top 5
            vpn_proxies = [f"http://{p}" for p in proxies]
            logger.info(f"✓ Fetched {len(vpn_proxies)} free proxies")
            return vpn_proxies
    except Exception as e:
        logger.warning(f"Could not fetch free proxies: {str(e)}")
    
    return []

def get_proxy_list(use_vpn: bool = False):
    """Get proxy list based on VPN mode"""
    # First check if user VPN is configured
    if USER_VPN_CONFIG["enabled"] and USER_VPN_CONFIG["proxy_url"]:
        logger.info(f"Using configured VPN: {USER_VPN_CONFIG['vpn_server']}")
        return [USER_VPN_CONFIG["proxy_url"], None]  # Add fallback
    
    if use_vpn or VPN_ENABLED:
        # Try to include VPN proxies
        proxies = fetch_free_vpn_proxies()
        if proxies:
            return proxies + [None]  # Add direct connection as fallback
        return VPN_PROXY_LIST
    return PROXY_LIST

def generate_token(url: str) -> str:
    """Generate a unique token for each URL paste (unlimited, one per link)"""
    token_id = str(uuid.uuid4())
    token_secret = secrets.token_urlsafe(32)
    token = f"{token_id}_{token_secret}"
    
    TOKEN_DB[token] = {
        "url": url,
        "created_at": datetime.now().isoformat(),
        "expires_at": (datetime.now() + timedelta(hours=24)).isoformat(),
        "views": 0,
        "downloaded": False,
        "platform": get_platform(url),
        "access_count": 0
    }
    
    return token

def validate_token(token: str) -> bool:
    """Validate if token is still active"""
    if token not in TOKEN_DB:
        return False
    
    token_data = TOKEN_DB[token]
    expires_at = datetime.fromisoformat(token_data["expires_at"])
    
    if datetime.now() > expires_at:
        del TOKEN_DB[token]
        return False
    
    return True

def get_url_from_token(token: str) -> Optional[str]:
    """Get URL from token"""
    if validate_token(token):
        TOKEN_DB[token]["access_count"] += 1
        return TOKEN_DB[token]["url"]
    return None

# ============================================
# ========== UTIL FUNCTIONS =================
# ============================================

def get_domain(url: str):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"

def get_platform(url: str):
    """Detect the platform from URL"""
    url_lower = url.lower()
    if 'youtube.com' in url_lower or 'youtu.be' in url_lower:
        return 'youtube'
    elif 'twitter.com' in url_lower or 'x.com' in url_lower:
        return 'twitter'
    elif 'facebook.com' in url_lower or 'fb.com' in url_lower:
        return 'facebook'
    elif 'instagram.com' in url_lower:
        return 'instagram'
    elif 'tiktok.com' in url_lower:
        return 'tiktok'
    else:
        return 'generic'

def build_headers(video_url: str, referer: str = None, platform: str = 'generic', incoming_range: str = None, proxy_index: int = 0):
    """Build platform-specific headers with bypass techniques"""
    domain = get_domain(video_url)
    
    if not referer:
        referer = domain
    
    # Rotating user agents for bypass
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    ]
    
    # Platform-specific headers
    headers = {
        "User-Agent": user_agents[proxy_index % len(user_agents)],
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "DNT": "1",
        "Cache-Control": "no-cache",
    }
    
    # Platform-specific overrides for bypass
    if platform == 'twitter' or platform == 'x':
        headers.update({
            "Authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
            "X-Requested-With": "XMLHttpRequest",
        })
    elif platform == 'instagram':
        headers.update({
            "X-Requested-With": "XMLHttpRequest",
            "X-Instagram-WWW-CLAIM": "0",
        })
    elif platform == 'terabox':
        headers.update({
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "X-Access-Token": "0",
        })
    elif platform == 'facebook':
        headers.update({
            "X-Requested-With": "XMLHttpRequest",
        })
    else:
        headers.update({
            "Referer": referer,
            "Origin": domain,
        })
    
    if incoming_range:
        headers["Range"] = incoming_range
    
    return headers

# ============================================
# ========== TERABOX HANDLER ================
# ============================================

def extract_terabox_info(url: str) -> Dict:
    """Extract video info from Terabox URL with advanced bypass and VPN support"""
    try:
        # Extract share_id from URL
        share_match = re.search(r'/s/([a-zA-Z0-9_-]+)', url)
        if not share_match:
            share_match = re.search(r'share_id=([^&]+)', url)
        
        if not share_match:
            return {
                "title": "Terabox Video",
                "url": url,
                "platform": "terabox",
                "status": "fallback"
            }
        
        share_id = share_match.group(1)
        
        # Try multiple Terabox domains and methods
        terabox_domains = [
            'https://www.terabox.com',
            'https://www.teraboxapp.com',
            'https://1024terabox.com',
            'https://www.1024terabox.com',
        ]
        
        # Advanced headers for Terabox
        terabox_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.google.com/",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        
        # Get VPN proxy if enabled
        proxy = USER_VPN_CONFIG.get("proxy_url") if USER_VPN_CONFIG["enabled"] else None
        proxies = {"http": proxy, "https": proxy} if proxy else {}
        
        for domain in terabox_domains:
            try:
                # Try direct URL access
                full_url = f"{domain}/s/{share_id}" if '/s/' not in url else url
                
                session = requests.Session()
                session.headers.update(terabox_headers)
                
                logger.info(f"Trying Terabox domain: {domain} with timeout 15s (VPN: {'enabled' if proxy else 'disabled'})")
                response = session.get(
                    full_url, 
                    timeout=15, 
                    allow_redirects=True, 
                    verify=False,
                    proxies=proxies if proxies else None
                )
                
                if response.status_code == 200:
                    # Try to extract video info
                    try:
                        # Look for video URL in page source
                        patterns = [
                            r'"url":"([^"]+?\.(?:mp4|webm|mov)[^"]*)"',
                            r'src=["\']([^"\']*(?:mp4|webm|mov)[^"\']*)["\']',
                            r'"dlink":"([^"]+)"',
                            r'data-video-url=["\']([^"\']+)["\']',
                        ]
                        
                        for pattern in patterns:
                            match = re.search(pattern, response.text, re.IGNORECASE)
                            if match:
                                video_url = match.group(1).replace('\\/', '/')
                                logger.info(f"✓ Found Terabox video URL from {domain}")
                                return {
                                    "title": "Terabox Video",
                                    "url": video_url,
                                    "platform": "terabox",
                                    "status": "success"
                                }
                        
                        # Title extraction
                        title_match = re.search(r'<title>([^<]+)</title>', response.text)
                        title = title_match.group(1) if title_match else "Terabox Video"
                        
                        logger.warning(f"No direct video URL found on {domain}, returning fallback")
                        
                    except Exception as e:
                        logger.warning(f"Error parsing {domain}: {str(e)}")
                        continue
                        
            except requests.Timeout:
                logger.warning(f"Timeout on {domain}, trying next...")
                continue
            except requests.ConnectionError as e:
                logger.warning(f"Connection error on {domain}: {str(e)}")
                continue
            except Exception as e:
                logger.warning(f"Error with {domain}: {str(e)}")
                continue
        
        # Fallback - return original URL
        logger.info("All Terabox methods failed, returning fallback")
        return {
            "title": "Terabox Video",
            "url": url,
            "platform": "terabox",
            "status": "fallback",
            "note": "Try downloading directly with our download feature"
        }
        
    except Exception as e:
        logger.error(f"Terabox extraction error: {str(e)}")
        return {
            "title": "Terabox Video",
            "url": url,
            "platform": "terabox",
            "status": "error",
            "error": str(e)
        }

def get_terabox_download_link(url: str) -> Optional[str]:
    """Get direct download link for Terabox with bypass and VPN support"""
    try:
        terabox_domains = [
            'https://www.terabox.com',
            'https://www.teraboxapp.com',
            'https://1024terabox.com',
        ]
        
        terabox_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.google.com/",
        }
        
        # Get VPN proxy if enabled
        proxy = USER_VPN_CONFIG.get("proxy_url") if USER_VPN_CONFIG["enabled"] else None
        proxies = {"http": proxy, "https": proxy} if proxy else {}
        
        share_match = re.search(r'/s/([a-zA-Z0-9_-]+)', url)
        if not share_match:
            return None
        
        share_id = share_match.group(1)
        
        for domain in terabox_domains:
            try:
                full_url = f"{domain}/s/{share_id}"
                session = requests.Session()
                session.headers.update(terabox_headers)
                
                # Use VPN proxy for Terabox request
                response = session.get(
                    full_url, 
                    timeout=15, 
                    allow_redirects=True, 
                    verify=False,
                    proxies=proxies if proxies else None
                )
                
                if response.status_code == 200:
                    patterns = [
                        r'"url":"([^"]+\.(?:mp4|webm|mov)[^"]*)"',
                        r'src=["\']([^"\']*(?:mp4|webm|mov)[^"\']*)["\']',
                        r'"dlink":"([^"]+)"',
                    ]
                    
                    for pattern in patterns:
                        match = re.search(pattern, response.text)
                        if match:
                            return match.group(1).replace('\\/', '/')
                            
            except Exception as e:
                logger.warning(f"Terabox download attempt failed on {domain}: {str(e)}")
                continue
        
        return None
        
    except Exception as e:
        logger.error(f"Terabox download link error: {str(e)}")
        return None

# def get_cookie_header():
#     if not HAS_BROWSER_COOKIES:
#         return None
#     try:
#         cj = browser_cookie3.chrome()
#         return cj
#     except Exception as e:
#         logger.warning(f"Could not get cookies: {e}")
#         return None

# ------------------ EXTRACT WITH yt-dlp (BEST METHOD) ------------------

def extract_with_ytdlp(url: str, platform: str = None, proxy: Optional[str] = None):
    """Extract video info using yt-dlp with bypass techniques"""
    ydl_opts = {
        "quiet": False,
        "no_warnings": False,
        "skip_download": True,
        "format": "best[ext=mp4]/best/best[ext=webm]",
        "noplaylist": True,
        "extract_flat": False,
        "socket_timeout": 20,
        "http_headers": build_headers(url, platform=platform),
    }
    
    if proxy:
        ydl_opts["proxy"] = proxy
    
    # Add platform-specific options
    if platform == 'twitter' or platform == 'x':
        ydl_opts["headers"] = build_headers(url, platform='twitter')
    elif platform == 'instagram':
        ydl_opts["headers"] = build_headers(url, platform='instagram')
    elif platform == 'terabox':
        ydl_opts["headers"] = build_headers(url, platform='terabox')
        ydl_opts["socket_timeout"] = 120  # Longer timeout for Terabox
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info
    except Exception as e:
        logger.error(f"yt-dlp extraction error: {str(e)}")
        return None

def extract_with_requests(url: str, platform: str = None) -> Dict:
    """Fallback extraction using direct requests for Terabox"""
    try:
        if platform == 'terabox':
            return extract_terabox_info(url)
        else:
            # Generic extraction
            session = requests.Session()
            headers = build_headers(url, platform=platform)
            response = session.get(url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                return {
                    "title": "Video",
                    "url": url,
                    "status": "extracted"
                }
    except Exception as e:
        logger.error(f"Requests extraction error: {str(e)}")
    
    return None

# ------------------ DOWNLOAD WITH yt-dlp (MOST RELIABLE) ------------------

def download_with_ytdlp(url: str, filepath: str, platform: str = None, proxy: Optional[str] = None):
    """Download video using yt-dlp with bypass and proxy support"""
    ydl_opts = {
        'outtmpl': filepath,
        'format': 'best[ext=mp4]/best/best[ext=webm]',
        'quiet': False,
        'no_warnings': False,
        'ignoreerrors': True,
        'nooverwrites': True,
        'continuedl': True,
        'socket_timeout': 30,
        'http_headers': build_headers(url, platform=platform),
    }
    
    if proxy:
        ydl_opts['proxy'] = proxy
    
    # Add platform-specific options
    if platform == 'twitter' or platform == 'x':
        ydl_opts['headers'] = build_headers(url, platform='twitter')
    elif platform == 'instagram':
        ydl_opts['headers'] = build_headers(url, platform='instagram')
    elif platform == 'terabox':
        ydl_opts['headers'] = build_headers(url, platform='terabox')
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Downloading with yt-dlp from: {url}")
            ydl.download([url])
        
        if os.path.exists(filepath):
            return True
        else:
            # Check if yt-dlp created a different filename
            import glob
            pattern = f"{DOWNLOAD_DIR}/*.mp4"
            files = glob.glob(pattern)
            if files:
                latest_file = max(files, key=os.path.getctime)
                if os.path.getctime(latest_file) > time.time() - 60:
                    import shutil
                    shutil.move(latest_file, filepath)
                    return True
        return False
    except Exception as e:
        logger.error(f"yt-dlp download failed: {str(e)}")
        return False

def download_direct(url: str, filepath: str, headers: dict, proxy: Optional[str] = None):
    """Download using direct requests with bypass support"""
    try:
        session = requests.Session()
        
        # Add retries
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        response = session.get(
            url,
            headers=headers,
            stream=True,
            timeout=60,
            allow_redirects=True,
            proxies={"http": proxy, "https": proxy} if proxy else {},
            verify=False
        )
        
        if response.status_code in [200, 206]:
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            
            with open(filepath, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size:
                            logger.info(f"Progress: {downloaded*100/total_size:.1f}%")
            
            return True
        return False
    except Exception as e:
        logger.error(f"Direct download failed: {str(e)}")
        return False

def stream_video_direct(url: str, headers: dict, proxy: Optional[str] = None):
    """Stream video directly without saving to disk (for serverless/mobile)"""
    try:
        session = requests.Session()
        
        # Add retries
        retry_strategy = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        response = session.get(
            url,
            headers=headers,
            stream=True,
            timeout=60,
            allow_redirects=True,
            proxies={"http": proxy, "https": proxy} if proxy else {},
            verify=False
        )
        
        if response.status_code in [200, 206]:
            def generate_chunks():
                for chunk in response.iter_content(chunk_size=256 * 1024):  # 256KB chunks
                    if chunk:
                        yield chunk
            
            return generate_chunks()
        return None
    except Exception as e:
        logger.error(f"Stream direct failed: {str(e)}")
        return None

# ------------------ HOME ------------------

@app.get("/", response_class=HTMLResponse)
def home():
    token_count = len(TOKEN_DB)
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Universal Video Downloader - Watch & Download</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
                background: linear-gradient(135deg, #0f172a 0%, #1e40af 100%);
                min-height: 100vh;
                padding: 20px;
            }}
            .container {{
                max-width: 900px;
                margin: 0 auto;
                background: white;
                border-radius: 20px;
                padding: 40px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            }}
            h1 {{
                color: #333;
                margin-bottom: 10px;
                font-size: 2.5em;
            }}
            .subtitle {{
                color: #666;
                margin-bottom: 30px;
                font-size: 1.1em;
            }}
            .input-group {{
                display: flex;
                gap: 10px;
                margin-bottom: 20px;
            }}
            input {{
                flex: 1;
                padding: 15px;
                font-size: 16px;
                border: 2px solid #e0e0e0;
                border-radius: 10px;
                transition: all 0.3s;
            }}
            input:focus {{
                outline: none;
                border-color: #1e40af;
                box-shadow: 0 0 0 3px rgba(30, 64, 175, 0.1);
            }}
            button {{
                padding: 15px 30px;
                font-size: 16px;
                background: linear-gradient(135deg, #1e40af 0%, #3b82f6 100%);
                color: white;
                border: none;
                border-radius: 10px;
                cursor: pointer;
                transition: transform 0.2s, box-shadow 0.2s;
                font-weight: 600;
            }}
            button:hover {{
                transform: translateY(-2px);
                box-shadow: 0 10px 20px rgba(30, 64, 175, 0.3);
            }}
            button:active {{
                transform: translateY(0);
            }}
            .features {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                gap: 20px;
                margin: 30px 0;
            }}
            .feature {{
                background: linear-gradient(135deg, #1e40af15 0%, #3b82f615 100%);
                padding: 20px;
                border-radius: 10px;
                border: 2px solid #1e40af30;
            }}
            .feature h3 {{
                color: #1e40af;
                margin-bottom: 8px;
            }}
            .feature p {{
                color: #666;
                font-size: 0.95em;
            }}
            .supported {{
                margin-top: 30px;
                padding: 20px;
                background: #f8f9fa;
                border-radius: 10px;
            }}
            .supported h3 {{
                margin-bottom: 15px;
                color: #333;
            }}
            .platforms {{
                display: flex;
                flex-wrap: wrap;
                gap: 10px;
            }}
            .platform-badge {{
                background: linear-gradient(135deg, #1e40af 0%, #3b82f6 100%);
                color: white;
                padding: 6px 14px;
                border-radius: 20px;
                font-size: 13px;
                font-weight: 500;
            }}
            .platform-badge.new {{
                background: linear-gradient(135deg, #28a745 0%, #20c997 100%);
                animation: pulse 2s infinite;
            }}
            @keyframes pulse {{
                0%, 100% {{ opacity: 1; }}
                50% {{ opacity: 0.7; }}
            }}
            .token-info {{
                background: #e7f3ff;
                border-left: 4px solid #007bff;
                padding: 15px;
                border-radius: 5px;
                margin: 20px 0;
                color: #004085;
            }}
            .token-count {{
                font-weight: bold;
                color: #667eea;
                font-size: 1.2em;
            }}

            @media (max-width: 600px) {{
                .container {{
                    padding: 20px;
                }}
                .input-group {{
                    flex-direction: column;
                }}
                h1 {{
                    font-size: 1.8em;
                }}
            }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🎬Video Downloader</h1>
            <p class="subtitle">Download & Watch videos</p>
            
            <form action="/info" method="get">
                <div class="input-group">
                    <input type="text" name="url" placeholder="Paste video URL..." required/>
                    <button type="submit">🚀 Fetch & Play</button>
                </div>
            </form>
            
            <div class="features">
                <div class="feature">
                    <h3>▶️ Play Now</h3>
                    <p>Watch videos directly in your browser with our advanced player</p>
                </div>
                <div class="feature">
                    <h3>💾 Download</h3>
                    <p>Download videos in best quality formats with one click</p>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

# ------------------ INFO PAGE ------------------

@app.get("/info", response_class=HTMLResponse)
def get_video_info(url: str = Query(...)):
    try:
        platform = get_platform(url)
        logger.info(f"Detected platform: {platform}, URL: {url[:50]}...")
        
        # Generate unique token for this URL
        token = generate_token(url)
        
        # Always use VPN proxy if configured (automatic, no user control needed)
        proxy = USER_VPN_CONFIG.get("proxy_url") if USER_VPN_CONFIG["enabled"] else None
        if proxy:
            logger.info(f"✓ Using VPN proxy for video extraction: {USER_VPN_CONFIG['vpn_server']}")
        
        # Try to extract with yt-dlp first, then fallback to requests
        info = extract_with_ytdlp(url, platform, proxy)
        if not info:
            info = extract_with_requests(url, platform)
        
        # For all platforms, allow fallback even if extraction fails
        # This enables users to still download/play even if extraction times out
        if not info:
            info = {
                "title": "Video",
                "formats": [],
                "status": "timeout_fallback"
            }
        
        title = info.get('title', 'Video')
        thumbnail = info.get('thumbnail', '')
        
        # Get available formats
        formats = []
        if 'formats' in info:
            for f in info.get("formats", []):
                if f.get("url"):
                    quality = f.get("format_note") or f.get("height") or "unknown"
                    if str(quality) != "unknown":
                        formats.append({
                            "quality": quality,
                            "ext": f.get("ext"),
                            "url": f.get("url"),
                            "filesize": f.get("filesize"),
                        })
        
        # Remove duplicates and sort
        unique_formats = []
        seen = set()
        for f in formats:
            if f['url'] not in seen:
                seen.add(f['url'])
                unique_formats.append(f)
        
        unique_formats.sort(key=lambda x: int(x['quality']) if str(x['quality']).isdigit() else 0, reverse=True)
        unique_formats = unique_formats[:10]
        
        encoded_url = quote(url, safe='')
        encoded_token = quote(token, safe='')
        encoded_title = quote(title, safe='')
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>{title} - Video Downloader</title>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                * {{
                    margin: 0;
                    padding: 0;
                    box-sizing: border-box;
                }}
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
                    background: linear-gradient(135deg, #0f172a 0%, #1e40af 100%);
                    min-height: 100vh;
                    padding: 20px;
                }}
                .container {{
                    max-width: 900px;
                    margin: 0 auto;
                    background: white;
                    border-radius: 20px;
                    overflow: hidden;
                    box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                }}
                .header {{
                    background: linear-gradient(135deg, #1e40af 0%, #3b82f6 100%);
                    padding: 20px;
                    color: white;
                }}
                .content {{
                    padding: 30px;
                }}
                .video-info {{
                    margin-bottom: 30px;
                }}
                .video-title {{
                    font-size: 1.8em;
                    margin-bottom: 15px;
                    color: #333;
                    word-break: break-word;
                }}
                .thumbnail {{
                    max-width: 100%;
                    border-radius: 10px;
                    margin-bottom: 20px;
                    box-shadow: 0 5px 15px rgba(0,0,0,0.1);
                }}
                .formats {{
                    margin-top: 30px;
                }}
                .formats h3 {{
                    color: #333;
                    margin-bottom: 15px;
                }}
                .format-card {{
                    background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
                    border-radius: 10px;
                    padding: 15px;
                    margin-bottom: 15px;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    transition: transform 0.2s, box-shadow 0.2s;
                    flex-wrap: wrap;
                    gap: 10px;
                    border-left: 4px solid #1e40af;
                }}
                .format-card:hover {{
                    transform: translateX(5px);
                    box-shadow: 0 5px 20px rgba(30, 64, 175, 0.2);
                }}
                .quality {{
                    font-weight: bold;
                    color: #1e40af;
                    font-size: 1.1em;
                }}
                .size {{
                    color: #666;
                    font-size: 0.9em;
                    margin-left: 10px;
                }}
                .actions {{
                    display: flex;
                    gap: 10px;
                    flex-wrap: wrap;
                }}
                .btn {{
                    padding: 10px 20px;
                    border: none;
                    border-radius: 8px;
                    cursor: pointer;
                    text-decoration: none;
                    display: inline-block;
                    font-size: 14px;
                    transition: all 0.3s;
                    font-weight: 600;
                }}
                .btn-play {{
                    background: linear-gradient(135deg, #28a745 0%, #20c997 100%);
                    color: white;
                }}
                .btn-play:hover {{
                    transform: translateY(-2px);
                    box-shadow: 0 5px 15px rgba(40, 167, 69, 0.3);
                }}
                .btn-download {{
                    background: linear-gradient(135deg, #ffc107 0%, #ff9800 100%);
                    color: white;
                }}
                .btn-download:hover {{
                    transform: translateY(-2px);
                    box-shadow: 0 5px 15px rgba(255, 193, 7, 0.3);
                }}
                .back-btn {{
                    display: inline-block;
                    padding: 10px 20px;
                    background: rgba(255,255,255,0.2);
                    color: white;
                    text-decoration: none;
                    border-radius: 5px;
                    transition: background 0.3s;
                }}
                .back-btn:hover {{
                    background: rgba(255,255,255,0.3);
                }}
                .token-badge {{
                    display: inline-block;
                    background: #e7f3ff;
                    color: #004085;
                    padding: 8px 12px;
                    border-radius: 5px;
                    font-size: 0.85em;
                    margin: 10px 0;
                    border-left: 4px solid #007bff;
                }}
                .token-badge code {{
                    background: rgba(0,0,0,0.1);
                    padding: 2px 6px;
                    border-radius: 3px;
                    font-family: monospace;
                }}
                .note {{
                    margin-top: 20px;
                    padding: 15px;
                    background: #d4edda;
                    border-left: 4px solid #28a745;
                    border-radius: 5px;
                    font-size: 0.95em;
                    color: #155724;
                }}
                @media (max-width: 600px) {{
                    .format-card {{
                        flex-direction: column;
                        text-align: center;
                    }}
                    .actions {{
                        justify-content: center;
                    }}
                    .video-title {{
                        font-size: 1.3em;
                    }}
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <a href="/" class="back-btn">← Back to Home</a>
                </div>
                <div class="content">
                    <div class="video-info">
                        
        """
        
        if thumbnail:
            html += f'<img src="{thumbnail}" class="thumbnail" onerror="this.style.display=\'none\'">'
        
        # Display title below thumbnail for verification
        html += f"""
                        <div style="background: linear-gradient(135deg, #e3f2fd 0%, #bbdefb 100%); padding: 20px; border-radius: 10px; margin: 20px 0; border-left: 5px solid #1e40af;">
                            <h2 class="video-title" style="margin: 0; color: #0d47a1; font-size: 1.6em; word-break: break-word;">
                                📹 {title}
                            </h2>
                        </div>
        """
        
        if not unique_formats:
            # If no formats found, show direct play option
            if platform == 'terabox':
                # Special handling for Terabox
                terabox_link = get_terabox_download_link(url)
                if terabox_link:
                    encoded_link = quote(terabox_link, safe='')
                    html += f"""
                            <div class="format-card">
                                <div>
                                    <span class="quality">📦 Terabox Video</span>
                                </div>
                                <div class="actions">
                                    <a href="/player?url={encoded_link}&token={encoded_token}&title={encoded_title}" class="btn btn-play">▶ Play Now</a>
                                    <a href="/download?url={encoded_link}&token={encoded_token}&title={encoded_title}" class="btn btn-download">💾 Download</a>
                                </div>
                            </div>
                    """
                else:
                    html += f"""
                            <div class="format-card">
                                <div>
                                    <span class="quality">📦 Terabox Video</span>
                                </div>
                                <div class="actions">
                                    <a href="/player?url={encoded_url}&token={encoded_token}&title={encoded_title}&use_ytdlp=true" class="btn btn-play">▶ Play Now</a>
                                    <a href="/download?url={encoded_url}&token={encoded_token}&title={encoded_title}&use_ytdlp=true" class="btn btn-download">💾 Download</a>
                                </div>
                            </div>
                    """
                    html += """
                        <div class="note">
                            💡 Using intelligent download methods. Terabox links are being extracted...
                        </div>
                    """
            else:
                html += f"""
                            <div class="format-card">
                                <div>
                                    <span class="quality">🎬 Best Quality</span>
                                </div>
                                <div class="actions">
                                    <a href="/player?url={encoded_url}&token={encoded_token}&title={encoded_title}&use_ytdlp=true" class="btn btn-play">▶ Play Now</a>
                                    <a href="/download?url={encoded_url}&token={encoded_token}&title={encoded_title}&use_ytdlp=true" class="btn btn-download">💾 Download</a>
                                </div>
                            </div>
                """
                fallback_msg = ""
                if info.get("status") == "timeout_fallback":
                    fallback_msg = """
                        <div class="note" style="background: #fff3cd; border-left-color: #ffc107; color: #856404;">
                            ⚠️ <strong>Server busy:</strong> The website is taking too long to respond. Try downloading directly - our multi-method system will extract the video!
                        </div>
                    """
                else:
                    fallback_msg = """
                        <div class="note">
                            💡 <strong>Note:</strong> Video will be extracted and made available for download.
                        </div>
                    """
                html += fallback_msg
        else:
            html += """
                    </div>
                    <div class="formats">
                        <h3>✨ Available Formats:</h3>
            """
            
            for f in unique_formats[:5]:  # Show top 5 formats
                filesize = f" • {f['filesize'] // 1024 // 1024}MB" if f.get('filesize') else ""
                encoded_format_url = quote(f['url'], safe='')
                
                html += f"""
                        <div class="format-card">
                            <div>
                                <span class="quality">{f['quality']}p</span>
                                <span class="size">{filesize}</span>
                            </div>
                            <div class="actions">
                                <a href="/player?url={encoded_format_url}&token={encoded_token}&title={encoded_title}" class="btn btn-play">▶ Play Now</a>
                                <a href="/download?url={encoded_format_url}&token={encoded_token}&title={encoded_title}" class="btn btn-download">💾 Download</a>
                            </div>
                        </div>
                """
            
            html += """
                    </div>
            """
        
        html += f"""
                    <div class="note">
                        💡 <strong>Pro Tips:</strong> If direct play doesn't work, try download. For mobile, save to camera roll.
                    </div>
                </div>
                <div style="position: fixed; bottom: 20px; right: 20px; font-size: 1.2em; font-weight: bold; color: #c62828; border: solid 3px red; padding: 12px 18px; border-radius: 8px; background: #ffebee; letter-spacing: 0.5px; box-shadow: 0 4px 12px rgba(198, 40, 40, 0.3); z-index: 1000;">
                    ❤️ Thank you for using this website!
                </div>
            </div>
        </body>
        </html>
        """
        
        return HTMLResponse(content=html)
        
    except Exception as e:
        logger.error(f"Error in /info: {str(e)}")
        error_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Error - Video Downloader</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    max-width: 600px;
                    margin: 50px auto;
                    padding: 20px;
                    background: #f5f5f5;
                }}
                .error {{
                    background: #f8d7da;
                    color: #721c24;
                    padding: 20px;
                    border-radius: 10px;
                    border: 1px solid #f5c6cb;
                }}
                .back-btn {{
                    display: inline-block;
                    padding: 10px 20px;
                    background: #007bff;
                    color: white;
                    text-decoration: none;
                    border-radius: 5px;
                    margin-top: 20px;
                }}
            </style>
        </head>
        <body>
            <div class="error">
                <h3>❌ Error fetching video</h3>
                <p>Error: {str(e)[:200]}</p>
                <a href="/" class="back-btn">← Try Another URL</a>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=error_html, status_code=400)

# ============================================
# ========== PLAY NOW ENDPOINT (NEW) ==========
# ============================================

@app.get("/player", response_class=HTMLResponse)
def play_now(url: str = Query(...), token: Optional[str] = Query(None), title: Optional[str] = Query("Video")):
    """Advanced video player with Play Now feature"""
    decoded_url = unquote(url)
    decoded_title = unquote(title)
    
    # Validate token if provided
    if token and not validate_token(unquote(token)):
        return HTMLResponse("""
        <html><body style="text-align:center;padding:50px;">
        <h2>❌ Token Expired</h2>
        <p>Your token has expired. <a href="/">← Go back and paste URL again</a></p>
        </body></html>
        """, status_code=401)
    
    player_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>▶ {decoded_title} - Video Player</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            body {{
                background: #000;
                display: flex;
                justify-content: center;
                align-items: center;
                min-height: 100vh;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                padding: 20px;
            }}
            .player-container {{
                width: 100%;
                max-width: 1200px;
                background: #111;
                border-radius: 15px;
                overflow: hidden;
                box-shadow: 0 20px 60px rgba(0,0,0,0.5);
            }}
            .video-wrapper {{
                position: relative;
                width: 100%;
                padding-bottom: 56.25%;
                height: 0;
                overflow: hidden;
            }}
            .video-wrapper video {{
                position: absolute;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: #000;
            }}
            .controls {{
                padding: 20px;
                background: linear-gradient(135deg, #1e40af 0%, #3b82f6 100%);
                display: flex;
                gap: 10px;
                justify-content: center;
                flex-wrap: wrap;
            }}
            .btn {{
                padding: 12px 24px;
                font-size: 14px;
                background: rgba(255,255,255,0.2);
                color: white;
                border: none;
                border-radius: 8px;
                cursor: pointer;
                transition: all 0.3s;
                font-weight: 600;
                display: flex;
                align-items: center;
                gap: 8px;
            }}
            .btn:hover {{
                background: rgba(255,255,255,0.3);
                transform: translateY(-2px);
            }}
            .info {{
                padding: 20px;
                background: #1a1a1a;
                color: #fff;
                text-align: center;
                border-top: 1px solid #333;
            }}
            .info-title {{
                font-size: 1.2em;
                margin-bottom: 10px;
            }}
            .info-meta {{
                font-size: 0.9em;
                color: #aaa;
            }}
            .loading {{
                display: none;
                text-align: center;
                padding: 40px;
                color: #fff;
            }}
            .spinner {{
                border: 4px solid #f3f3f3;
                border-top: 4px solid #1e40af;
                border-radius: 50%;
                width: 40px;
                height: 40px;
                animation: spin 1s linear infinite;
                margin: 0 auto 20px;
            }}
            @keyframes spin {{
                0% {{ transform: rotate(0deg); }}
                100% {{ transform: rotate(360deg); }}
            }}
            @media (max-width: 768px) {{
                .controls {{
                    flex-direction: column;
                }}
                .btn {{
                    width: 100%;
                    justify-content: center;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="player-container">
            <div class="video-wrapper">
                <div class="loading" id="loading">
                    <div class="spinner"></div>
                    <p>Loading video...</p>
                </div>
                <video id="videoPlayer" controls autoplay preload="auto">
                    <source src="{decoded_url}" type="video/mp4">
                    Your browser doesn't support HTML5 video.
                </video>
            </div>
            <div class="controls">
                <button class="btn" onclick="playVideo()">▶ Play</button>
                <button class="btn" onclick="pauseVideo()">⏸ Pause</button>
                <button class="btn" onclick="toggleFullscreen()">🖥 Fullscreen</button>
                <button class="btn" onclick="downloadVideo()">💾 Download</button>
                <button class="btn" onclick="shareVideo()">📤 Share</button>
                <button class="btn" onclick="goHome()">🏠 Home</button>
            </div>
            <div class="info">
                <div class="info-title">📹 {decoded_title}</div>
                <div class="info-meta">🎬 Playing directly from source • Right-click to save video</div>
            </div>
        </div>
        
        <script>
            const video = document.getElementById('videoPlayer');
            const loading = document.getElementById('loading');
            
            video.addEventListener('loadstart', function() {{
                loading.style.display = 'block';
            }});
            
            video.addEventListener('loadeddata', function() {{
                loading.style.display = 'none';
            }});
            
            function playVideo() {{
                video.play();
            }}
            
            function pauseVideo() {{
                video.pause();
            }}
            
            function toggleFullscreen() {{
                if (video.requestFullscreen) {{
                    video.requestFullscreen();
                }} else if (video.webkitRequestFullscreen) {{
                    video.webkitRequestFullscreen();
                }}
            }}
            
            function downloadVideo() {{
                const a = document.createElement('a');
                a.href = '{decoded_url}';
                a.download = '{decoded_title.replace(" ", "_")}.mp4';
                a.target = '_blank';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
            }}
            
            function shareVideo() {{
                const text = 'Check out this video: {decoded_title}';
                const url = window.location.href;
                if (navigator.share) {{
                    navigator.share({{title: '{decoded_title}', text: text, url: url}});
                }} else {{
                    alert('Share this link: ' + url);
                }}
            }}
            
            function goHome() {{
                window.location.href = '/';
            }}
        </script>
    </body>
    </html>
    """
    
    return HTMLResponse(content=player_html)

# ============================================
# ========== DIRECT PLAYER (OLD) =============
# ============================================

@app.get("/direct")
def direct_player(url: str):
    """Direct embedded player (legacy)"""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Video Player</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{
                margin: 0;
                padding: 20px;
                background: #000;
                display: flex;
                justify-content: center;
                align-items: center;
                min-height: 100vh;
                font-family: Arial, sans-serif;
            }}
            .container {{
                max-width: 1200px;
                width: 100%;
                background: #111;
                border-radius: 10px;
                overflow: hidden;
            }}
            video {{
                width: 100%;
                background: #000;
                display: block;
            }}
            .controls {{
                padding: 20px;
                background: #1a1a1a;
                display: flex;
                gap: 10px;
                justify-content: center;
                flex-wrap: wrap;
            }}
            button {{
                padding: 10px 20px;
                font-size: 14px;
                background: #007bff;
                color: white;
                border: none;
                border-radius: 5px;
                cursor: pointer;
                transition: background 0.3s;
            }}
            button:hover {{
                background: #0056b3;
            }}
            .info {{
                padding: 20px;
                background: #1a1a1a;
                color: #fff;
                text-align: center;
                border-top: 1px solid #333;
            }}
            a {{
                color: #007bff;
                text-decoration: none;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <video id="video" controls autoplay>
                <source src="{url}" type="video/mp4">
                Your browser doesn't support video playback.
            </video>
            <div class="controls">
                <button onclick="document.getElementById('video').play()">▶ Play</button>
                <button onclick="document.getElementById('video').pause()">⏸ Pause</button>
                <button onclick="downloadVideo()">💾 Download</button>
                <button onclick="window.location.href='/'">🏠 Home</button>
            </div>
            <div class="info">
                💡 Right-click on the video and select "Save Video As..." to download
            </div>
        </div>
        <script>
            function downloadVideo() {{
                var a = document.createElement('a');
                a.href = '{url}';
                a.download = 'video.mp4';
                a.target = '_blank';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
            }}
        </script>
    </body>
    </html>
    """

# ------------------ DOWNLOAD ENDPOINT (STREAMING VERSION FOR MOBILE & SERVERLESS) ------------------

@app.get("/download")
def download_video(url: str, token: Optional[str] = Query(None), title: Optional[str] = Query(None), use_ytdlp: Optional[str] = Query("false")):
    """Stream video directly to browser/mobile without saving to disk (Vercel-compatible)"""
    try:
        decoded_url = unquote(url)
        decoded_token = unquote(token) if token else None
        decoded_title = unquote(title) if title else None
        platform = get_platform(decoded_url)
        
        logger.info(f"Download request - Platform: {platform}, URL: {decoded_url[:60]}... (Streaming)")
        
        # Validate token if provided
        if decoded_token:
            if not validate_token(decoded_token):
                return HTTPException(status_code=401, detail="Token expired or invalid")
            logger.info(f"Valid token: {decoded_token[:20]}...")
        
        # Generate filename
        if decoded_title:
            safe_title = re.sub(r'[<>:"/\\|?*]', '', decoded_title)[:100]
            filename = f"{safe_title}.mp4"
        else:
            filename = f"video_{int(time.time())}.mp4"
        
        # Get proxy list
        proxy_list = get_proxy_list(use_vpn=True)
        
        # ===== SPECIAL HANDLING FOR TERABOX (Direct Link) =====
        if platform == 'terabox':
            logger.info("Terabox: Finding direct download link...")
            direct_link = get_terabox_download_link(decoded_url)
            if direct_link:
                logger.info(f"Terabox: Found direct link, streaming...")
                headers = build_headers(direct_link, platform='generic')
                for proxy in proxy_list:
                    stream = stream_video_direct(direct_link, headers, proxy)
                    if stream:
                        logger.info("✓ Streaming Terabox video directly")
                        return StreamingResponse(
                            stream,
                            media_type="video/mp4",
                            headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
                        )
        
        # ===== METHOD 1: Try yt-dlp for social platforms =====
        if use_ytdlp == "true" or platform in ['youtube', 'twitter', 'facebook', 'instagram', 'tiktok', 'reddit', 'x']:
            logger.info("Method 1: Attempting yt-dlp streaming...")
            for proxy_idx, proxy in enumerate(proxy_list):
                try:
                    ydl_opts = {
                        'format': 'best[ext=mp4]/best/best[ext=webm]',
                        'quiet': True,
                        'no_warnings': True,
                        'ignoreerrors': True,
                        'socket_timeout': 30,
                        'http_headers': build_headers(decoded_url, platform=platform),
                    }
                    
                    if proxy:
                        ydl_opts['proxy'] = proxy
                    
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(decoded_url, download=False)
                        if info and info.get('url'):
                            video_url = info['url']
                            stream_headers = build_headers(video_url, platform=platform)
                            stream = stream_video_direct(video_url, stream_headers, proxy)
                            if stream:
                                logger.info(f"✓ Streaming with yt-dlp (proxy: {proxy or 'direct'})")
                                return StreamingResponse(
                                    stream,
                                    media_type="video/mp4",
                                    headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
                                )
                except Exception as e:
                    logger.warning(f"yt-dlp Method 1 failed: {str(e)}")
                    continue
        
        # ===== METHOD 2: Direct streaming with rotating headers =====
        logger.info("Method 2: Attempting direct streaming...")
        for attempt, proxy in enumerate(proxy_list):
            headers = build_headers(decoded_url, platform=platform, referer=decoded_url, proxy_index=attempt)
            stream = stream_video_direct(decoded_url, headers, proxy)
            
            if stream:
                logger.info(f"✓ Streaming directly (proxy: {proxy or 'direct'})")
                return StreamingResponse(
                    stream,
                    media_type="video/mp4",
                    headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
                )
        
        # ===== METHOD 3: Try with different referers =====
        logger.info("Method 3: Trying with alternative referers...")
        referer_options = [decoded_url, get_domain(decoded_url), "https://google.com", "https://facebook.com"]
        for referer in referer_options:
            headers = build_headers(decoded_url, platform=platform, referer=referer)
            for proxy in proxy_list:
                stream = stream_video_direct(decoded_url, headers, proxy)
                
                if stream:
                    logger.info(f"✓ Streaming with referer: {referer}")
                    return StreamingResponse(
                        stream,
                        media_type="video/mp4",
                        headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
                    )
        
        # ===== METHOD 4: yt-dlp with all proxies (final attempt) =====
        logger.info("Method 4: Final yt-dlp attempt with all proxies...")
        for proxy in proxy_list:
            try:
                ydl_opts = {
                    'format': 'best[ext=mp4]/best/best[ext=webm]',
                    'quiet': True,
                    'no_warnings': True,
                    'socket_timeout': 60,
                }
                
                if proxy:
                    ydl_opts['proxy'] = proxy
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(decoded_url, download=False)
                    if info and info.get('url'):
                        video_url = info['url']
                        stream_headers = build_headers(video_url, platform=platform)
                        stream = stream_video_direct(video_url, stream_headers, proxy)
                        if stream:
                            logger.info(f"✓ Streaming with yt-dlp final attempt")
                            return StreamingResponse(
                                stream,
                                media_type="video/mp4",
                                headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
                            )
            except Exception as e:
                logger.warning(f"yt-dlp final attempt failed: {str(e)}")
                continue
        
        logger.error(f"All streaming methods failed for: {decoded_url}")
        raise HTTPException(
            status_code=400,
            detail="Could not stream video. The URL may be invalid, region-restricted, or the platform may require authentication."
        )
        
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Download error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Download error: {str(e)}")

@app.get("/direct")
def direct_player(url: str):
    """Direct embedded player (legacy)"""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Video Player</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{
                margin: 0;
                padding: 20px;
                background: #000;
                display: flex;
                justify-content: center;
                align-items: center;
                min-height: 100vh;
                font-family: Arial, sans-serif;
            }}
            .container {{
                max-width: 1200px;
                width: 100%;
                background: #111;
                border-radius: 10px;
                overflow: hidden;
            }}
            video {{
                width: 100%;
                background: #000;
                display: block;
            }}
            .controls {{
                padding: 20px;
                background: #1a1a1a;
                display: flex;
                gap: 10px;
                justify-content: center;
                flex-wrap: wrap;
            }}
            button {{
                padding: 10px 20px;
                font-size: 14px;
                background: #007bff;
                color: white;
                border: none;
                border-radius: 5px;
                cursor: pointer;
                transition: background 0.3s;
            }}
            button:hover {{
                background: #0056b3;
            }}
            .info {{
                padding: 20px;
                background: #1a1a1a;
                color: #fff;
                text-align: center;
                border-top: 1px solid #333;
            }}
            a {{
                color: #007bff;
                text-decoration: none;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <video id="video" controls autoplay>
                <source src="{url}" type="video/mp4">
                Your browser doesn't support video playback.
            </video>
            <div class="controls">
                <button onclick="document.getElementById('video').play()">▶ Play</button>
                <button onclick="document.getElementById('video').pause()">⏸ Pause</button>
                <button onclick="downloadVideo()">💾 Download</button>
                <button onclick="window.location.href='/'">🏠 Home</button>
            </div>
            <div class="info">
                💡 Right-click on the video and select "Save Video As..." to download
            </div>
        </div>
        <script>
            function downloadVideo() {{
                var a = document.createElement('a');
                a.href = '{url}';
                a.download = 'video.mp4';
                a.target = '_blank';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
            }}
        </script>
    </body>
    </html>
    """

@app.get("/stream")
def stream_video(url: str, request: Request):
    """Stream video with proxy and bypass - Vercel-compatible"""
    try:
        decoded_url = unquote(url)
        platform = get_platform(decoded_url)
        
        range_header = request.headers.get("range")
        proxy_list = get_proxy_list(use_vpn=True)
        
        # Try with VPN proxies first
        for proxy_index, proxy in enumerate(proxy_list):
            headers = build_headers(decoded_url, platform=platform, referer=decoded_url, proxy_index=proxy_index, incoming_range=range_header)
            
            try:
                stream = stream_video_direct(decoded_url, headers, proxy)
                if stream:
                    logger.info(f"✓ Streaming video (proxy: {proxy or 'direct'})")
                    return StreamingResponse(
                        stream,
                        status_code=200,
                        media_type="video/mp4",
                        headers={
                            "Accept-Ranges": "bytes",
                            "Cache-Control": "no-cache",
                        }
                    )
            except Exception as e:
                logger.warning(f"Stream attempt {proxy_index + 1} failed: {str(e)}")
                continue
        
        # Fallback: Try yt-dlp for extraction
        logger.info("Fallback: Attempting yt-dlp extraction for streaming...")
        for proxy in proxy_list:
            try:
                ydl_opts = {
                    'format': 'best[ext=mp4]/best',
                    'quiet': True,
                    'no_warnings': True,
                }
                
                if proxy:
                    ydl_opts['proxy'] = proxy
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(decoded_url, download=False)
                    if info and info.get('url'):
                        video_url = info['url']
                        stream_headers = build_headers(video_url, platform=platform)
                        stream = stream_video_direct(video_url, stream_headers, proxy)
                        if stream:
                            logger.info(f"✓ Streaming via yt-dlp extraction")
                            return StreamingResponse(
                                stream,
                                media_type="video/mp4",
                                headers={"Cache-Control": "no-cache"}
                            )
            except Exception as e:
                logger.warning(f"yt-dlp streaming failed: {str(e)}")
                continue
        
        # Fallback to direct player
        logger.warning("All streaming methods failed, redirecting to direct player")
        return RedirectResponse(url=f"/direct?url={quote(decoded_url)}")
        
    except Exception as e:
        logger.error(f"Streaming error: {str(e)}")
        return RedirectResponse(url=f"/direct?url={quote(url)}")

# ============================================
# ========== API ENDPOINTS ===================
# ============================================

# VPN Configuration Endpoints

@app.post("/api/vpn/configure")
def configure_vpn(
    vpn_server: str = Query(...),
    vpn_type: str = Query(...),
    username: str = Query(...),
    password: str = Query(...),
    port: Optional[int] = Query(None)
):
    """Configure VPN for video extraction"""
    try:
        logger.info(f"Configuring VPN: {vpn_type} at {vpn_server}")
        
        # For SSTP VPN, we need to use a proxy URL format
        # Since SSTP is a protocol, we'll use it to construct a proxy endpoint
        proxy_url = None
        vpn_port = port or 443  # Default to 443 for SSTP
        
        if vpn_type.upper() == "SSTP":
            # SSTP VPN proxy format (basic auth)
            # SSTP typically uses port 443
            vpn_port = port or 443
            proxy_url = f"http://{username}:{password}@{vpn_server}:{vpn_port}"
        elif vpn_type.upper() == "SOCKS5":
            vpn_port = port or 1080
            proxy_url = f"socks5://{username}:{password}@{vpn_server}:{vpn_port}"
        elif vpn_type.upper() == "SOCKS4":
            vpn_port = port or 1080
            proxy_url = f"socks4://{username}:{password}@{vpn_server}:{vpn_port}"
        elif vpn_type.upper() == "HTTP":
            vpn_port = port or 8080
            proxy_url = f"http://{username}:{password}@{vpn_server}:{vpn_port}"
        else:
            vpn_port = port or 8080
            proxy_url = f"http://{username}:{password}@{vpn_server}:{vpn_port}"
        
        # Store VPN configuration
        USER_VPN_CONFIG["enabled"] = True
        USER_VPN_CONFIG["vpn_server"] = vpn_server
        USER_VPN_CONFIG["vpn_type"] = vpn_type
        USER_VPN_CONFIG["username"] = username
        USER_VPN_CONFIG["password"] = password
        USER_VPN_CONFIG["port"] = vpn_port
        USER_VPN_CONFIG["proxy_url"] = proxy_url
        
        logger.info(f"✓ VPN configured successfully: {vpn_type} @ {vpn_server}:{vpn_port}")
        
        return JSONResponse({
            "status": "success",
            "message": f"VPN configured: {vpn_type}",
            "vpn_server": vpn_server,
            "vpn_type": vpn_type,
            "port": vpn_port,
            "enabled": True
        })
    except Exception as e:
        logger.error(f"VPN configuration error: {str(e)}")
        return JSONResponse({
            "status": "error",
            "message": str(e)
        }, status_code=400)

@app.get("/api/vpn/status")
def get_vpn_status():
    """Get current VPN status"""
    return JSONResponse({
        "vpn_enabled": USER_VPN_CONFIG["enabled"],
        "vpn_server": USER_VPN_CONFIG["vpn_server"],
        "vpn_type": USER_VPN_CONFIG["vpn_type"],
        "username": USER_VPN_CONFIG["username"] if USER_VPN_CONFIG["enabled"] else None,
    })

@app.post("/api/vpn/disable")
def disable_vpn():
    """Disable VPN"""
    USER_VPN_CONFIG["enabled"] = False
    logger.info("VPN disabled")
    return JSONResponse({
        "status": "success",
        "message": "VPN disabled",
        "vpn_enabled": False
    })

@app.get("/api/tokens")
def get_tokens_info():
    """Get token stats"""
    return JSONResponse({
        "total_active_tokens": len(TOKEN_DB),
        "tokens": [
            {
                "token": token[:20] + "...",
                "platform": data["platform"],
                "created_at": data["created_at"],
                "expires_at": data["expires_at"],
                "access_count": data["access_count"]
            }
            for token, data in list(TOKEN_DB.items())[-10:]
        ]
    })

@app.post("/api/generate-token")
def api_generate_token(url: str = Query(...)):
    """Generate token for URL"""
    token = generate_token(url)
    return JSONResponse({
        "token": token,
        "url": url,
        "platform": get_platform(url),
        "expires_in_hours": 24
    })

@app.get("/api/validate-token")
def api_validate_token(token: str = Query(...)):
    """Validate token"""
    valid = validate_token(unquote(token))
    return JSONResponse({"valid": valid})

# ============================================
# ========== CLEANUP & STARTUP ==============
# ============================================
@app.on_event("startup")
def startup_event():
    """Initialize on startup"""
    logger.info("=" * 70)
    logger.info("🚀 UNIVERSAL VIDEO DOWNLOADER & PLAYER - ADVANCED VERSION")
    logger.info("=" * 70)
    logger.info("📍 Server started successfully!")
    logger.info(f"🌐 Access at: http://localhost:8000")
    logger.info(f"📊 Token System: Unlimited tokens, one per URL paste")
    logger.info(f"🎬 Features: Play Now • Download • Bypass • Terabox Support")
    
    # Log VPN status
    if USER_VPN_CONFIG["enabled"] and USER_VPN_CONFIG["proxy_url"]:
        logger.info(f"🔐 VPN STATUS: ACTIVE")
        logger.info(f"   Protocol: {USER_VPN_CONFIG['vpn_type']}")
        logger.info(f"   Server: {USER_VPN_CONFIG['vpn_server']}:{USER_VPN_CONFIG.get('port', 443)}")
        logger.info(f"   Username: {USER_VPN_CONFIG['username']}")
        logger.info(f"   All video extraction & downloads will use VPN proxy")
    else:
        logger.info(f"🔐 VPN STATUS: NOT CONFIGURED")
        logger.info(f"   Use /api/vpn/configure to setup VPN")
    
    logger.info("=" * 70)

@app.on_event("shutdown")
def cleanup_old_downloads():
    """Clean up old downloads on shutdown"""
    import glob
    one_hour_ago = time.time() - 3600
    cleaned = 0
    for filepath in glob.glob(f"{DOWNLOAD_DIR}/*.mp4"):
        try:
            if os.path.getctime(filepath) < one_hour_ago:
                os.remove(filepath)
                cleaned += 1
                logger.info(f"Cleaned up: {filepath}")
        except Exception as e:
            logger.warning(f"Cleanup failed: {e}")
    
    logger.info(f"Cleanup complete: {cleaned} files removed")

if __name__ == "__main__":
    import uvicorn
    print("=" * 70)
    print("🚀 Universal Video Downloader & Player")
    print("=" * 70)
    print(f"📍 Local:   http://localhost:8000")
    print("=" * 70)
    print("✨ FEATURES:")
    print("   ✅ Play Now - Watch videos directly")
    print("   ✅ Download - Multiple quality options")
    print("=" * 70)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")