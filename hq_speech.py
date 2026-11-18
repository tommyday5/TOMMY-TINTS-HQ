"""Owner-only neural speech. No audio/text stored; bounded per-owner usage."""
import os
import time
import requests
from hq_auth_store import auth_db

INSTRUCTIONS = ('Speak with a natural, refined British English accent. Calm, warm, intelligent and professional. '
 'Use fluid conversational phrasing, subtle expression and natural pauses. Avoid a robotic cadence, '
 'exaggerated formality or theatrical delivery. Do not imitate any actor or fictional character.')

def configured():
    return bool(os.environ.get('OPENAI_API_KEY'))

def reserve(owner):
    now=time.time()
    with auth_db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS speech_usage (owner TEXT NOT NULL, created REAL NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM speech_usage WHERE created<?',(now-86400,))
        minute=db.execute('SELECT count(*) FROM speech_usage WHERE owner=? AND created>?',(owner,now-60)).fetchone()[0]
        day=db.execute('SELECT count(*) FROM speech_usage WHERE owner=?',(owner,)).fetchone()[0]
        if minute>=5 or day>=60: return False
        db.execute('INSERT INTO speech_usage VALUES (?,?)',(owner,now))
        return True

def generate(text):
    if not isinstance(text,str) or not text.strip() or len(text)>2000: raise ValueError('Speech text must contain 1–2000 characters')
    if not configured(): raise RuntimeError('Neural speech is not configured')
    response=requests.post('https://api.openai.com/v1/audio/speech',headers={'Authorization':'Bearer '+os.environ['OPENAI_API_KEY']},json={'model':os.environ.get('OPENAI_TTS_MODEL','gpt-4o-mini-tts'),'voice':'cedar','input':text,'instructions':INSTRUCTIONS,'response_format':'mp3','speed':1.0},timeout=35,stream=True)
    try:
        response.raise_for_status()
        if response.headers.get('Content-Type','').split(';')[0] not in ('audio/mpeg','audio/mp3','application/octet-stream'): raise RuntimeError('Invalid speech response')
        chunks=[];size=0
        for chunk in response.iter_content(16384):
            size+=len(chunk)
            if size>3_000_000: raise RuntimeError('Speech response exceeds limit')
            chunks.append(chunk)
        if not size: raise RuntimeError('Empty speech response')
        return b''.join(chunks)
    finally: response.close()
