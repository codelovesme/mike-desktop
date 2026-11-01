"""Local silence detection and bounded, cancellable speech playback."""
import array
import base64
import math
import os
from pathlib import Path
import shutil
import subprocess
import struct
import tempfile
import time

TAP_SECONDS = .35
SILENCE_SECONDS = 1.2
IDLE_SECONDS = 30


class Detector:
    def __init__(self):
        self.frames = 0
        self.speech = False
        self.last_voice = None
        self.started = time.monotonic()
        self.noise = .003

    def inspect(self, path, now=None):
        now = time.monotonic() if now is None else now
        # Capture tools leave RIFF/data lengths at zero until they close
        # the file. Read complete PCM bytes already written, not those lengths.
        try:
            raw = Path(path).read_bytes()
        except FileNotFoundError:
            raw = b''
        data = b''
        offset, valid = 12, False
        if raw[:4] == b'RIFF' and raw[8:12] == b'WAVE':
            while offset+8<=len(raw):
                tag,size = struct.unpack_from('<4sI',raw,offset)
                offset += 8
                if tag==b'fmt ' and size>=16 and offset+16<=len(raw):
                    codec,channels,rate,_,_,bits = struct.unpack_from('<HHIIHH',raw,offset)
                    valid = codec in (1,0xfffe) and (channels,rate,bits)==(1,16000,16)
                if tag==b'data':
                    if not valid:raise ValueError('conversation needs 16 kHz mono PCM recording')
                    data = raw[offset+self.frames*2:]
                    break
                offset += size+(size&1)
        data = data[:len(data)//2*2]
        self.frames += len(data)//2
        for offset in range(0, len(data)-1, 3200):
            samples = array.array('h', data[offset:offset+3200])
            if os.sys.byteorder != 'little': samples.byteswap()
            rms = math.sqrt(sum(v*v for v in samples)/len(samples))/32768
            threshold = max(.012, self.noise*3)
            if rms >= threshold:
                self.speech = True
                self.last_voice = now
            elif not self.speech:
                self.noise = min(.015, .95*self.noise + .05*rms)
        if self.speech and self.last_voice is not None and now-self.last_voice >= SILENCE_SECONDS:
            return 'utterance'
        if not self.speech and now-self.started >= IDLE_SECONDS:
            return 'idle'
        return ''


class Player:
    TOOLS = [('pw-play', []), ('paplay', []), ('aplay', ['-q']),
             ('ffplay', ['-nodisp', '-autoexit', '-loglevel', 'error'])]

    def __init__(self):
        self.process = None
        self.path = None

    @classmethod
    def available(cls):
        return any(shutil.which(name) for name, _ in cls.TOOLS)

    def start(self, encoded, format):
        self.stop()
        if format not in ('wav', 'mp3', 'ogg', 'flac'):
            raise ValueError('unsupported speech audio format')
        if not isinstance(encoded, str) or len(encoded) > 44*1024*1024:
            raise ValueError('speech audio exceeds playback limit')
        data = base64.b64decode(encoded, validate=True)
        if not data or len(data) > 32*1024*1024:
            raise ValueError('empty or oversized speech audio')
        for name, args in self.TOOLS:
            if shutil.which(name):
                fd, self.path = tempfile.mkstemp(prefix='mike-speech-', suffix='.'+format)
                with os.fdopen(fd, 'wb') as f:f.write(data)
                try:
                    self.process = subprocess.Popen([name,*args,self.path],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                except OSError:
                    self.stop()
                    raise
                return
        raise ValueError('install PipeWire, PulseAudio, ALSA or ffplay for spoken replies')

    def stop(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
            self.process = None
        if self.path:
            Path(self.path).unlink(missing_ok=True)
            self.path = None
