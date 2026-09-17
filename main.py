import os
import re
import urllib.parse
from fastapi import FastAPI, HTTPException, Query
import httpx

# FastAPI instance MUST be named 'app' for uvicorn main:app
app = FastAPI(title="Eclipse TorBox Bridge")

# Environment Variables (Configured on Render)
raw_jackett_url = os.getenv("JACKETT_URL", "http://localhost:9117").strip()
if raw_jackett_url and not raw_jackett_url.startswith(("http://", "https://")):
    JACKETT_URL = f"https://{raw_jackett_url}"
else:
    JACKETT_URL = raw_jackett_url

JACKETT_API_KEY = os.getenv("JACKETT_API_KEY", "").strip()
TORBOX_API_KEY = os.getenv("TORBOX_API_KEY", "").strip()

# -------------------------------------------------------------------
# 1. Root & Manifest Endpoints
# -------------------------------------------------------------------
@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Eclipse TorBox Bridge",
        "manifest": "/manifest.json"
    }

@app.get("/manifest.json")
async def manifest():
    return {
        "id": "com.user.torbox-debrid",
        "name": "TorBox FLAC Bridge",
        "version": "1.0.0",
        "description": "Searches Jackett, checks TorBox cache, and streams 24-bit/16-bit FLACs.",
        "resources": ["search", "stream"],
        "types": ["track", "album"]
    }

# -------------------------------------------------------------------
# 2. Metadata Cleaning via MusicBrainz (Spelling & Metadata)
# -------------------------------------------------------------------
async def correct_song_name(query: str):
    artist_hint = ""
    title_hint = query
    if " - " in query:
        parts = query.split(" - ", 1)
        artist_hint = parts[0].strip()
        title_hint = parts[1].strip()

    clean_title = re.sub(r'[^\w\s]', '', title_hint)
    mb_query = f'recording:"{clean_title}"'
    if artist_hint:
        clean_artist = re.sub(r'[^\w\s]', '', artist_hint)
        mb_query += f' AND artist:"{clean_artist}"'

    url = "https://musicbrainz.org/ws/2/recording"
    headers = {"User-Agent": "EclipseAddonBridge/1.0.0 ( contact@yourdomain.com )"}
    params = {"query": mb_query, "fmt": "json", "limit": 5}
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(url, params=params, headers=headers)
            if res.status_code == 200:
                recordings = res.json().get("recordings", [])
                if recordings:
                    best = recordings[0]
                    title = best.get("title", title_hint)
                    
                    artist_credits = best.get("artist-credit", [])
                    artist = artist_credits[0].get("name", "Various Artists") if artist_credits else "Various Artists"
                    
                    releases = best.get("releases", [])
                    album = releases[0].get("title", "") if releases else ""
                    
                    return f"{artist} - {title}", title, artist, album
    except Exception:
        pass

    if artist_hint:
        return f"{artist_hint} - {title_hint}", title_hint, artist_hint, ""
    return query, query.capitalize(), "Various Artists", ""

# -------------------------------------------------------------------
# 3. Search Endpoint
# -------------------------------------------------------------------
@app.get("/search")
async def search(q: str = Query(...)):
    corrected_query, title, artist, album = await correct_song_name(q)
    
    # Sanitize string to prevent breaking route paths
    safe_id_string = re.sub(r'[:/?#\[\]@!$&\'()*+,;=]', '', corrected_query)
    
    return {
        "results": [
            {
                "id": f"track_{safe_id_string}",
                "title": title,
                "artist": artist,
                "album": album,
                "format": "flac"
            }
        ]
    }

# -------------------------------------------------------------------
# 4. Stream Endpoint (Sanitized Query -> Jackett -> TorBox -> Stream)
# -------------------------------------------------------------------
@app.get("/stream/{track_id:path}")
async def get_stream(track_id: str):
    decoded_id = urllib.parse.unquote(track_id)
    raw_query = decoded_id.replace("track_", "")
    
    # Strip characters that break torrent indexers
    jackett_query = re.sub(r'[:/\\?#]', ' ', raw_query)
    jackett_query = ' '.join(jackett_query.split())
    
    # A. Search Jackett API
    jackett_endpoint = f"{JACKETT_URL.rstrip('/')}/api/v2.0/indexers/all/results"
    params = {
        "apikey": JACKETT_API_KEY,
        "Query": f"{jackett_query} FLAC"
    }
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            res = await client.get(jackett_endpoint, params=params)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to connect to Jackett at {JACKETT_URL}: {str(e)}")

        if res.status_code != 200:
            raise HTTPException(status_code=502, detail="Jackett lookup failed")
        
        results = res.json().get("Results", [])
        if not results:
            raise HTTPException(status_code=404, detail=f"No FLAC torrents found for query: {jackett_query}")
        
        # B. Prioritize 24-bit FLAC over 16-bit FLAC
        selected_magnet = None
        
        # Tier 1 Search: 24-bit / Hi-Res
        for item in results:
            title = item.get("Title", "").lower()
            if any(k in title for k in ["24bit", "24-bit", "24 bit", "hires", "hi-res"]):
                selected_magnet = item.get("MagnetUri") or item.get("Link")
                if selected_magnet:
                    break
        
        # Tier 2 Fallback: General FLAC (16-bit)
        if not selected_magnet:
            for item in results:
                if "flac" in item.get("Title", "").lower():
                    selected_magnet = item.get("MagnetUri") or item.get("Link")
                    if selected_magnet:
                        break

        if not selected_magnet:
            raise HTTPException(status_code=404, detail="Suitable FLAC releases not found")

       # C. Send Link to TorBox (Handles both raw Magnet URIs and HTTP .torrent URLs)
        torbox_headers = {"Authorization": f"Bearer {TORBOX_API_KEY}"}
        
        if selected_magnet.startswith("magnet:?"):
            # Direct Magnet URI
            payload = {"magnet": selected_magnet, "seed": "1", "allow_zip": "false"}
            add_torrent_res = await client.post(
                "https://api.torbox.app/v1/api/torrents/createtorrent",
                headers=torbox_headers,
                data=payload
            )
        else:
            # HTTP .torrent file URL: Download .torrent bytes and upload as file
            torrent_file_res = await client.get(selected_magnet, follow_redirects=True)
            if torrent_file_res.status_code != 200:
                raise HTTPException(status_code=502, detail="Failed to fetch .torrent file from indexer")
            
            files = {
                "file": ("download.torrent", torrent_file_res.content, "application/x-bittorrent")
            }
            add_torrent_res = await client.post(
                "https://api.torbox.app/v1/api/torrents/createtorrent",
                headers=torbox_headers,
                files=files,
                data={"seed": "1", "allow_zip": "false"}
            )
        
        torbox_data = add_torrent_res.json()
        if not torbox_data.get("success"):
            raise HTTPException(status_code=500, detail=f"TorBox error: {torbox_data.get('detail', 'Unknown error')}")

        # D. Fetch Direct Download / Stream Link from TorBox
        info_res = await client.get(
            f"https://api.torbox.app/v1/api/torrents/mylist?id={torrent_id}",
            headers=torbox_headers
        )
        info_data = info_res.json().get("data", {})
        
        if isinstance(info_data, list) and len(info_data) > 0:
            info_data = info_data[0]
            
        files = info_data.get("files", [])
        
        # Find the primary FLAC audio file inside the torrent
        audio_file_id = None
        for f in files:
            if f.get("name", "").lower().endswith(".flac"):
                audio_file_id = f.get("id")
                break
                
        if audio_file_id is None and files:
            audio_file_id = files[0].get("id")

        # E. Generate streamable link
        link_res = await client.get(
            f"https://api.torbox.app/v1/api/torrents/requestdl?token={TORBOX_API_KEY}&torrent_id={torrent_id}&file_id={audio_file_id}&zip=false"
        )
        
        stream_url = link_res.json().get("data")
        if not stream_url:
            raise HTTPException(status_code=500, detail="Could not retrieve stream URL from TorBox")

        # F. Return playable HTTP URL to Eclipse
        return {
            "url": stream_url,
            "format": "flac",
            "container": "flac"
        }
