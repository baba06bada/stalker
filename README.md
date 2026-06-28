# LGTV IPTV Proxy

WebOS 2.x (LG TV) icin IPTV proxy sunucusu. MPEG-TS stream'lerini TV'nin native `<video>` elementi ile oynatir.

**Hic hls.js / MSE / HLS gerekmez.** Ham TS verisi dogrudan HTTP chunked olarak gonderilir, TV hardware decoder ile oynatir.

## OZELLIKLER

- **Stalker/Portal** destegi (`/tslive`) — create_link ile kanal acma
- **Xtream** destegi (`/tsproxy`) — herhangi bir TS URL'sini proxy'den gecirme
- **Hata yok, buffer yok, segment yok** — saf TS stream
- Session reuse ile rate-limit korumasi
- 7/24 calisir (VPS, Raspberry Pi, NAS)

## KULLANIM

### 1. Calistirma

```bash
# Linux/Mac
python3 proxy_server.py

# Windows
python proxy_server.py
```

TV'den: `http://SUNUCU_IP:8000/stalker.html`

### 2. Stalker/Portal Kanallari

`stalker.html`'de portal URL + MAC gir, baglan. Kanal tiklayinca `/tslive` uzerinden oynatir.

### 3. Xtream Kanallari

Xtream player'inda kanal oynatma kodunu degistir:

```javascript
// ESKI (hls.js ile - WebOS'ta calismaz):
var hls = new Hls();
hls.loadSource('http://lefanten.com:8080/live/kullanici/sifre/123');
hls.attachMedia(video);
hls.on(Hls.Events.MANIFEST_PARSED, function() { video.play(); });

// YENI (proxy ile - WebOS'ta calisir):
var streamUrl = 'http://lefanten.com:8080/live/kullanici/sifre/123';
var proxyUrl = 'http://192.168.0.60:8000/tsproxy?url=' + encodeURIComponent(streamUrl);
video.type = 'video/MP2T';
video.src = proxyUrl;
video.play();
```

`.m3u8` playlist'lerindeki stream URL'lerini de proxy'den gecirebilirsin:

```
http://SUNUCU_IP:8000/tsproxy?url=http%3A%2F%2Flefanten.com%3A8080%2Flive%2Fkullanici%2Fsifre%2F123
```

## DOSYALAR

| Dosya | Aciklama |
|---|---|
| `proxy_server.py` | Proxy sunucu (calistirilacak ana dosya) |
| `stalker.html` | Web arayuzu (portal/STB kanallari icin) |
| `data.js` | Portal bilgilerin (TOKEN, MAC) |
| `start_proxy.py` | Kolay baslatma scripti |

## KURULUM

### VPS (Ubuntu/Debian)

```bash
sudo apt update && sudo apt install python3 -y
git clone https://github.com/KULLANICI/lg-iptv-proxy.git
cd lg-iptv-proxy
python3 proxy_server.py
```

Arkaplanda calistirmak icin:

```bash
screen -dmS proxy python3 proxy_server.py
```

### Raspberry Pi

```bash
sudo apt update && sudo apt install python3 -y
git clone https://github.com/KULLANICI/lg-iptv-proxy.git
cd lg-iptv-proxy
python3 proxy_server.py
```

### Windows

```cmd
python proxy_server.py
```

## NOTLAR

- Proxy herhangi bir MPEG-TS stream'ini (`.ts`, `live/...`, `ch/...`) proxy'den gecirebilir
- 8000 portu kullanilir (degistirmek icin `PORT = 8000` satirini duzenle)
- WebOS 2.x (LG TV 2015-2016) ile test edildi
- Stalker portal kullanmiyorsan `data.js`'deki bilgileri girme gerekmez
