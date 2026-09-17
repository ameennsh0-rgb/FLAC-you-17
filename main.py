async def correct_song_name(query: str):
    # Check if user/app sent "Artist - Song" format
    artist_hint = ""
    title_hint = query
    if " - " in query:
        parts = query.split(" - ", 1)
        artist_hint = parts[0].strip()
        title_hint = parts[1].strip()

    # Build Lucene query for MusicBrainz
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
                    
                    # Extract artist name
                    artist_credits = best.get("artist-credit", [])
                    artist = artist_credits[0].get("name", "Unknown Artist") if artist_credits else "Unknown Artist"
                    
                    # Extract album release title
                    releases = best.get("releases", [])
                    album = releases[0].get("title", "") if releases else ""
                    
                    return f"{artist} - {title}", title, artist, album
    except Exception:
        pass

    # Direct fallback if MusicBrainz API returns no results
    if artist_hint:
        return f"{artist_hint} - {title_hint}", title_hint, artist_hint, ""
    return query, query.capitalize(), "Various Artists", ""
