import asyncio
import subprocess
import edge_tts


def synth(text: str, voice: str, rate: str, mp3_path, wav_path):
    asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(str(mp3_path)))
    subprocess.run(["ffmpeg", "-y", "-i", str(mp3_path), "-ar", "44100", "-ac", "2", str(wav_path)],
                   check=True, capture_output=True)


def probe_duration(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], check=True, capture_output=True, text=True)
    return float(out.stdout.strip())
