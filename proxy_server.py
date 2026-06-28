#!C:\Users\babab\AppData\Local\Programs\Python\Python312-32\python.exe
"""LGTV IPTV Proxy Server
Calistir: python proxy_server.py
TV'den ac: http://BILGISAYAR_IP:8000/stalker.html

Stream akisi:
  1. Browser /play?cmd=...&mac=...&token=...&portal=...  -> create_link ile MPEG-TS URL al
  2. Proxy MPEG-TS stream'ini segmentlere boler
  3. Browser hls.js ile .m3u8 playlist + segmentleri ceker
  4. hls.js MPEG-TS'yi fMP4'e cevirip <video>'da oynatir
"""
import http.server
import urllib.request
import urllib.parse
import os, sys, socket, re, json, threading, time, uuid, hashlib

PORT = 8000
SEGMENT_DURATION = 3.0       # her segment ~3 saniye
MAX_SEGMENTS = 8             # hafizada tutulacak max segment
SEGMENT_CLEANUP_INTERVAL = 3 # temizlik araligi (saniye)

# === Aktif stream session'lari ===
active_streams = {}           # {session_id: StreamSession}
streams_lock = threading.Lock()

class StreamSession:
    def __init__(self, session_id, stream_url, playlist_url):
        self.id = session_id
        self.stream_url = stream_url
        self.playlist_url = playlist_url
        self.segments = []            # [(timestamp, data_bytes), ...]
        self.lock = threading.Lock()
        self.running = True
        self.last_access = time.time()
        self.buf = b''
        self.seg_counter = 0
        self.seq_counter = 0          # HLS sequence number
        self.cur_seg = []             # current segment TS packets list
        self.cur_seg_bytes = 0        # current segment byte count
        self.min_seg_bytes = 384000   # ~2s minimum segment size before splitting
        self.pat = None               # cached PAT packet (bytes)
        self.pmt = None               # cached PMT packet (bytes)
        self.pmt_pid = None           # PID of the PMT
        self.video_pid = None         # PID of video stream
        self.audio_pid = None         # PID of audio stream
        self.found_keyframe = False   # whether current seg has a keyframe
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()
        # Background cleanup thread
        self.cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self.cleanup_thread.start()

    def _parse_packet(self, pkt):
        """Parse a single 188-byte TS packet and return info dict."""
        if len(pkt) != 188 or pkt[0] != 0x47:
            return None
        pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
        pusi = (pkt[1] & 0x40) != 0   # payload_unit_start_indicator
        afc = (pkt[3] >> 4) & 3        # adaptation_field_control
        has_adapt = afc in (2, 3)
        has_payload = afc in (1, 3)
        return {
            'pid': pid,
            'pusi': pusi,
            'has_adapt': has_adapt,
            'has_payload': has_payload,
            'raw': pkt,
        }

    def _parse_pat(self, pkt):
        """Parse PAT to find PMT PID. Returns PMT PID or None."""
        if pkt[0] != 0x47 or ((pkt[1] & 0x1f) << 8 | pkt[2]) != 0x0000:
            return None
        if not (pkt[1] & 0x40):  # no payload start
            return None
        afc = (pkt[3] >> 4) & 3
        offset = 4
        if afc in (2, 3):
            afl = pkt[4]
            offset += 1 + afl
        if not (afc in (1, 3)):
            return None
        pay = pkt[offset:]
        if len(pay) < 8:
            return None
        ptr = pay[0]
        sec_start = 1 + ptr  # table_id position in pay
        if sec_start + 1 >= len(pay) or pay[sec_start] != 0x00:
            return None
        sec_len = ((pay[sec_start+1] & 0x0f) << 8) | pay[sec_start+2]
        sec_end = sec_start + 3 + sec_len  # one past end of section (incl CRC)
        idx = sec_start + 8  # skip header fields to first program entry
        while idx + 4 <= len(pay) and idx + 4 <= sec_end:
            prog_num = (pay[idx] << 8) | pay[idx+1]
            pmt_pid = ((pay[idx+2] & 0x1f) << 8) | pay[idx+3]
            if prog_num != 0:
                return pmt_pid
            idx += 4
        return None

    def _parse_pmt(self, pkt):
        """Parse PMT to find video and audio PIDs."""
        if pkt[0] != 0x47:
            return None, None
        pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
        if pid != self.pmt_pid:
            return None, None
        if not (pkt[1] & 0x40):
            return None, None
        afc = (pkt[3] >> 4) & 3
        offset = 4
        if afc in (2, 3):
            afl = pkt[4]
            offset += 1 + afl
        if not (afc in (1, 3)):
            return None, None
        pay = pkt[offset:]
        if len(pay) < 12:
            return None, None
        ptr = pay[0]
        sec_start = 1 + ptr  # table_id position in pay
        if sec_start + 1 >= len(pay) or pay[sec_start] != 0x02:
            return None, None
        sec_len = ((pay[sec_start+1] & 0x0f) << 8) | pay[sec_start+2]
        sec_end = sec_start + 3 + sec_len
        idx = sec_start + 8  # skip to PCR_PID (after table_id(1)+sec_len(2)+prog_num(2)+version(1)+section(2)=8)
        idx += 2  # PCR_PID (2 bytes, not needed)
        info_len = ((pay[idx] & 0x0f) << 8) | pay[idx+1]
        idx += 2 + info_len

        vpid = None
        apid = None
        while idx + 5 <= len(pay) and idx + 5 <= sec_end:
            stype = pay[idx]
            epid = ((pay[idx+1] & 0x1f) << 8) | pay[idx+2]
            es_info_len = ((pay[idx+3] & 0x0f) << 8) | pay[idx+4]
            idx += 5 + es_info_len
            if stype == 0x1b:  # H.264/AVC video
                vpid = epid
            elif stype == 0x24:  # HEVC
                if vpid is None:
                    vpid = epid
            elif stype in (0x0f, 0x11, 0x03, 0x04):  # AAC, MPEG audio
                if apid is None:
                    apid = epid
        return vpid, apid

    def _find_idr(self, pkt, pid):
        """Check if a TS packet (video PID) contains an IDR NAL.
        Returns True if IDR found."""
        if pid is None:
            return False
        pkt_pid = ((pkt[1] & 0x1f) << 8) | pkt[2]
        if pkt_pid != pid:
            return False
        afc = (pkt[3] >> 4) & 3
        offset = 4
        if afc in (2, 3):
            afl = pkt[4]
            offset += 1 + afl
        if not (afc in (1, 3)):
            return False
        # Search for NAL start codes 0x00 0x00 0x01 in payload
        pay = pkt[offset:]
        # Search for start codes
        for i in range(len(pay) - 3):
            if pay[i] == 0x00 and pay[i+1] == 0x00 and pay[i+2] == 0x01:
                nal_type = pay[i+3] & 0x1f
                if nal_type == 5:  # IDR frame
                    return True
        return False

    def _reader(self):
        TS_LEN = 188
        try:
            req = urllib.request.Request(self.stream_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=60) as resp:
                while self.running:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.last_access = time.time()
                    self.buf += chunk

                    # Process complete 188-byte packets
                    while len(self.buf) >= TS_LEN:
                        pkt = self.buf[:TS_LEN]
                        self.buf = self.buf[TS_LEN:]

                        if pkt[0] != 0x47:
                            continue  # skip bad sync

                        info = self._parse_packet(pkt)
                        if info is None:
                            continue

                        pid = info['pid']

                        # --- Track PAT/PMT ---
                        if pid == 0x0000 and info['pusi']:
                            pmt_pid = self._parse_pat(pkt)
                            if pmt_pid is not None:
                                self.pmt_pid = pmt_pid
                                self.pat = pkt  # cache PAT

                        if self.pmt_pid is not None and pid == self.pmt_pid and info['pusi']:
                            vpid, apid = self._parse_pmt(pkt)
                            if vpid is not None:
                                self.video_pid = vpid
                            if apid is not None:
                                self.audio_pid = apid
                            self.pmt = pkt  # cache PMT

                        # --- IDR detection ---
                        is_key = self._find_idr(pkt, self.video_pid)

                        # --- Add packet to current segment ---
                        self.cur_seg.append(pkt)
                        self.cur_seg_bytes += TS_LEN

                        if is_key:
                            self.found_keyframe = True

                        # --- Finalize segment when we have enough data AND hit a keyframe ---
                        MAX_SEGMENT_BYTES = 1024000  # ~5s max
                        enough_data = self.cur_seg_bytes >= self.min_seg_bytes
                        force_finalize = self.cur_seg_bytes >= MAX_SEGMENT_BYTES
                        if (force_finalize or (enough_data and self.found_keyframe)) and self.pat is not None and self.pmt is not None:

                            segment_data = b''.join(self.cur_seg)

                            with self.lock:
                                self.segments.append((time.time(), segment_data))
                                self.seg_counter += 1
                                if len(self.segments) > MAX_SEGMENTS:
                                    self.segments.pop(0)
                                self.seq_counter += 1

                            # Start new segment with cached PAT/PMT
                            self.cur_seg = [self.pat, self.pmt]
                            self.cur_seg_bytes = TS_LEN * 2
                            self.found_keyframe = False

        except Exception as e:
            print(f"[StreamSession {self.id}] Reader error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.running = False

    def _cleanup_loop(self):
        while self.running:
            time.sleep(SEGMENT_CLEANUP_INTERVAL)
            # Stop if inactive for 30 seconds
            if time.time() - self.last_access > 30:
                print(f"[StreamSession {self.id}] Timed out")
                self.running = False

    def get_playlist(self):
        with self.lock:
            seq = self.seq_counter
            n = len(self.segments)
            lines = [
                '#EXTM3U',
                '#EXT-X-VERSION:3',
                '#EXT-X-PLAYLIST-TYPE:LIVE',
                '#EXT-X-MEDIA-SEQUENCE:' + str(seq - n if seq >= n else 0),
                '#EXT-X-ALLOW-CACHE:NO',
                '#EXT-X-TARGETDURATION:3',
            ]
            for i in range(n):
                idx = seq - n + i
                dur = SEGMENT_DURATION
                lines.append(f'#EXTINF:{dur:.3f},')
                lines.append(f'/hls_segment?id={self.id}&seg={idx}')
            return '\n'.join(lines) + '\n'

    def get_segment(self, seg_id):
        with self.lock:
            self.last_access = time.time()
            seq = self.seq_counter
            n = len(self.segments)
            first_seq = seq - n
            if seg_id >= first_seq and seg_id < seq:
                idx = seg_id - first_seq
                if 0 <= idx < n:
                    return self.segments[idx][1]
        return None

    def stop(self):
        self.running = False


class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == '/play':
            self.handle_play(qs)
        elif path == '/playlist':
            self.handle_playlist(qs)
        elif path == '/hls_segment':
            self.handle_hls_segment(qs)
        elif path == '/tslive':
            self.handle_ts_live(qs)
        elif path == '/tsproxy':
            self.handle_ts_proxy(qs)
        elif self.path.startswith('/proxy?'):
            self.handle_old_proxy()
        else:
            super().do_GET()

    def handle_old_proxy(self):
        qs = self.path.split('?', 1)[1]
        params = urllib.parse.parse_qs(qs)
        target = params.get('url', [''])[0]
        if not target:
            self.send_error(400, 'Missing url parameter')
            return
        try:
            req = urllib.request.Request(target, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            ct = resp.headers.get('Content-Type', 'application/json')
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(('Proxy hatasi: ' + str(e)).encode('utf-8'))

    def handle_play(self, qs):
        """Browser'dan: /play?cmd=...&mac=...&token=...&portal=...
        Stream session olustur, .m3u8 playlist'i dondur.
        Ayni cmd icin ayni session kullanilir (native HLS refresh'te yeni session acilmaz).
        """
        cmd = qs.get('cmd', [''])[0]
        mac_addr = qs.get('mac', [''])[0]
        token = qs.get('token', [''])[0]
        portal = qs.get('portal', [''])[0]

        if not cmd or not mac_addr or not token or not portal:
            self.send_error(400, 'Missing parameters')
            return

        try:
            cmd_hash = hashlib.md5(cmd.encode()).hexdigest()[:8]

            # Mevcut session'u kontrol et
            with streams_lock:
                session = active_streams.get(cmd_hash) if cmd_hash in active_streams else None

            if session is None or not session.running:
                # Yeni session olustur
                api_params = urllib.parse.urlencode({
                    'type': 'itv', 'action': 'create_link',
                    'cmd': cmd, 'mac': mac_addr, 'token': token
                })
                api_url = portal + 'portal.php?' + api_params
                req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                d = json.loads(data.decode('utf-8'))
                stream_cmd = d.get('js', {}).get('cmd', '')
                if not stream_cmd:
                    raise Exception('create_link hata verdi: ' + data[:200])

                ts_url = stream_cmd.replace('ffmpeg ', '', 1)

                # Stream testi
                test_req = urllib.request.Request(ts_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(test_req, timeout=8) as test_resp:
                    test_data = test_resp.read(188)
                    if len(test_data) < 188 or test_data[0] != 0x47:
                        raise Exception('Stream MPEG-TS degil (0x%02X)' % (test_data[0] if test_data else 0))

                session_id = str(uuid.uuid4())[:8]
                session = StreamSession(session_id, ts_url, '')
                with streams_lock:
                    active_streams[cmd_hash] = session
                    active_streams[session_id] = session

                # Ilk segmenti bekle (max 10sn)
                playlist = session.get_playlist()
                for _ in range(40):
                    if '/hls_segment' in playlist:
                        break
                    time.sleep(0.25)
                    playlist = session.get_playlist()
                    if not session.running:
                        break

                if '/hls_segment' not in playlist:
                    raise Exception('Stream baslatilamadi (timeout)')
            else:
                # Mevcut session'dan playlist'i hizlica don
                playlist = session.get_playlist()

            data = playlist.encode('utf-8')
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(data)

        except Exception as e:
            print(f"[handle_play] Error: {e}")
            import traceback
            traceback.print_exc()
            self.send_response(502)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(('Hata: ' + str(e)).encode('utf-8'))

    def handle_playlist(self, qs):
        """Return current .m3u8 for a session (for hls.js live refresh)."""
        session_id = qs.get('id', [''])[0]
        if not session_id:
            self.send_error(400, 'Missing id')
            return
        with streams_lock:
            session = active_streams.get(session_id)
        if not session:
            self.send_error(404, 'Session not found')
            return
        # If no segments yet, wait up to 8s for first one
        playlist = session.get_playlist()
        if '/hls_segment' not in playlist:
            for _ in range(32):
                time.sleep(0.25)
                playlist = session.get_playlist()
                if '/hls_segment' in playlist:
                    break
        data = playlist.encode('utf-8')
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(data)

    def handle_hls_segment(self, qs):
        """Browser'dan: /hls_segment?id=<session_id>&seg=<number>
        Segment data'sini dondur.
        """
        session_id = qs.get('id', [''])[0]
        seg_str = qs.get('seg', [''])[0]

        if not session_id or not seg_str:
            self.send_error(400, 'Missing id or seg')
            return

        try:
            seg_id = int(seg_str)
        except ValueError:
            self.send_error(400, 'Invalid seg')
            return

        with streams_lock:
            session = active_streams.get(session_id)

        if not session:
            self.send_error(404, 'Session not found')
            return

        data = session.get_segment(seg_id)
        if data is None:
            # Segment henuz hazir degil - bekle ve dene
            for _ in range(20):
                time.sleep(0.25)
                data = session.get_segment(seg_id)
                if data is not None:
                    break

        if data is None:
            # Try to get the latest segment instead
            data = session.get_segment(session.seq_counter - 1)
            if data is None:
                self.send_error(503, 'Segment not ready')
                return

        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Type', 'video/MP2T')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'public, max-age=60')
        self.end_headers()
        self.wfile.write(data)

    def handle_ts_live(self, qs):
        """Raw MPEG-TS stream - dogrudan <video> elementi ile oynatilir.
        Hic HLS/MSE/hls.js olmadan calisir.
        """
        cmd = qs.get('cmd', [''])[0]
        mac_addr = qs.get('mac', [''])[0]
        token = qs.get('token', [''])[0]
        portal = qs.get('portal', [''])[0]

        if not cmd or not mac_addr or not token or not portal:
            self.send_error(400, 'Missing parameters')
            return

        headers_sent = False
        try:
            # Ayni cmd icin stream URL'ini cachele (rate-limit'i onle)
            cmd_hash = hashlib.md5(cmd.encode()).hexdigest()[:8]
            with streams_lock:
                cached = active_streams.get('url:' + cmd_hash)
                if cached and len(cached) > 1 and (time.time() - cached[1]) < 30:
                    ts_url = cached[0]
                else:
                    ts_url = None

            if not ts_url:
                api_params = urllib.parse.urlencode({
                    'type': 'itv', 'action': 'create_link',
                    'cmd': cmd, 'mac': mac_addr, 'token': token
                })
                api_url = portal + 'portal.php?' + api_params
                req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                d = json.loads(data.decode('utf-8'))
                stream_cmd = d.get('js', {}).get('cmd', '')
                if not stream_cmd:
                    raise Exception('create_link hata verdi: ' + data[:200])
                ts_url = stream_cmd.replace('ffmpeg ', '', 1)
                with streams_lock:
                    active_streams['url:' + cmd_hash] = (ts_url, time.time())

            # Chunked response baslat
            self.send_response(200)
            self.send_header('Content-Type', 'video/MP2T')
            self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            headers_sent = True

            # Stream URL'e baglan ve veriyi dogrudan aktar
            stream_req = urllib.request.Request(ts_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(stream_req, timeout=300) as ts_resp:
                while True:
                    chunk = ts_resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(('%X\r\n' % len(chunk)).encode('ascii'))
                    self.wfile.write(chunk)
                    self.wfile.write(b'\r\n')
                    self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')

        except ConnectionError as e:
            # TV kanal degistirdi veya sayfayi kapatti - normal, sessizce bitir
            if headers_sent:
                try:
                    self.wfile.write(b'0\r\n\r\n')
                except Exception:
                    pass
        except Exception as e:
            print(f"[handle_ts_live] Error: {e}")
            if not headers_sent:
                try:
                    self.send_response(502)
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(('Hata: ' + str(e)).encode('utf-8'))
                except Exception:
                    pass

    def handle_ts_proxy(self, qs):
        """Generic TS proxy: /tsproxy?url=ENCODED_STREAM_URL
        Herhangi bir MPEG-TS stream URL'sini <video> elementi ile oynatilabilir hale getirir.
        Xtream, Stalker, M3U — her kaynaktan URL'yi alir.
        """
        stream_url = qs.get('url', [''])[0]
        if not stream_url:
            self.send_error(400, 'Missing url parameter')
            return

        headers_sent = False
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'video/MP2T')
            self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            headers_sent = True

            req = urllib.request.Request(stream_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=300) as resp:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(('%X\r\n' % len(chunk)).encode('ascii'))
                    self.wfile.write(chunk)
                    self.wfile.write(b'\r\n')
                    self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')

        except ConnectionError:
            if headers_sent:
                try:
                    self.wfile.write(b'0\r\n\r\n')
                except Exception:
                    pass
        except Exception as e:
            print(f"[handle_ts_proxy] Error: {e}")
            if not headers_sent:
                try:
                    self.send_response(502)
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(('Hata: ' + str(e)).encode('utf-8'))
                except Exception:
                    pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()

    def log_message(self, fmt, *args):
        try:
            msg = f"[{self.date_time_string()}] {self.client_address[0]} {' '.join(str(a) for a in args)}"
        except Exception:
            msg = f"[{self.date_time_string()}] {self.client_address[0]} (log error)"
        print(msg)


# Periyodik temizlik - oluy session'lari kaldir
def cleanup_loop():
    while True:
        time.sleep(15)
        to_remove = []
        now = time.time()
        with streams_lock:
            for sid, sess in active_streams.items():
                if sid.startswith('url:'):
                    # URL cache: expire after 60s
                    if now - sess[1] > 60:
                        to_remove.append(sid)
                elif not sess.running:
                    to_remove.append(sid)
            for sid in to_remove:
                del active_streams[sid]


if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    
    # Start cleanup thread
    ct = threading.Thread(target=cleanup_loop, daemon=True)
    ct.start()

    http.server.HTTPServer.allow_reuse_address = False
    server = http.server.HTTPServer(('0.0.0.0', PORT), ProxyHandler)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.254.254.254', 1))
        ip = s.getsockname()[0]
    except:
        ip = '127.0.0.1'
    finally:
        s.close()
    print('=' * 50)
    print('LGTV IPTV Proxy Server basladi!')
    print('=' * 50)
    print(f'Yerel:  http://localhost:{PORT}/stalker.html')
    print(f'TV icin: http://{ip}:{PORT}/stalker.html')
    print()
    print('Durdurmak icin Ctrl+C')
    print('=' * 50)
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nSunucu durduruldu.')
        server.server_close()
