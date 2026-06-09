# BTK/ESB Domain Monitor Telegram Bot

Bu bot, kayıtlı domainler için BTK/ESB erişim engeli durumunu takip eder. Domain için erişim engeli kararı görülürse veya daha önce görülen engel kalkarsa Telegram üzerinden bildirim gönderir.

Varsayılan kontrol aralığı 5 dakikadır.

## Özellikler

- `/start`
- `/add domain.com`
- `/remove domain.com`
- `/list`
- `/check domain.com`
- `/help`
- Domainleri SQLite veritabanında saklar.
- Kayıtlı domainleri her 5 dakikada bir BTK sorgusu ile kontrol eder.
- Sadece durum değiştiğinde bildirim gönderir.
- Komut handlerları `python-telegram-bot` v20+ ile uyumlu şekilde `block=False` çalışır.
- Uzun süren BTK/OCR sorguları komut cevaplarını kilitlemez.
- `BOT_TOKEN` önce Railway environment variable üzerinden, yoksa `.env` dosyasından okunur.
- Railway üzerinde Dockerfile ile çalışmaya hazırdır.

## Nasıl Çalışır?

Bot domain erişilebilirliğini HTTP ile test etmez. Bunun yerine BTK sorgu sonucunu okur ve şu durumları saklar:

- `ENGEL VAR`: BTK sorgusunda erişim engeli/tedbir/mahkeme kararı benzeri karar metni bulundu.
- `ENGEL YOK`: BTK sorgusunda uygulanmış karar bulunmadı.
- `BILINMIYOR`: BTK sorgusu yapılamadı veya sonuç yorumlanamadı.

Bildirim yalnızca `ENGEL VAR` ile `ENGEL YOK` arasında durum değişirse gönderilir. `BILINMIYOR` geçici sorgu hatası kabul edilir ve engel kalktı/engel geldi bildirimi üretmez.

## Önemli Notlar

- BTK sorgu sayfası doğrulama/OCR akışı içerdiği için proje `BTKSorgu` Python paketini kullanır.
- Docker imajı içinde `tesseract-ocr` kuruludur.
- Railway'de tek instance/replica çalıştırın. Birden fazla instance aynı domainleri kontrol ederse çift bildirim gönderebilir.
- Railway'de kalıcı SQLite için volume kullanın. Volume yoksa deploy/restart sonrası veritabanı kaybolabilir.

## Environment Variables

```env
BOT_TOKEN=BOTFATHER_TOKENINIZ
DATABASE_PATH=domain_monitor.db
CHECK_INTERVAL_SECONDS=300
BTK_QUERY_TIMEOUT_SECONDS=300
MAX_CONCURRENT_CHECKS=3
```

Railway'de en az `BOT_TOKEN` eklenmelidir.

## Kurulum

Python 3.10 veya daha yeni bir sürüm gerekir.

Linux/macOS:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python main.py
```

## Railway Kurulumu

1. Projeyi GitHub'a yükleyin.
2. Railway'de yeni proje oluşturun.
3. GitHub reposunu Railway'e bağlayın.
4. Railway Variables bölümüne `BOT_TOKEN` ekleyin.
5. Kalıcı SQLite için Railway volume ekleyip `/data` yoluna mount edin.

Railway için önerilen değişkenler:

```env
BOT_TOKEN=BOTFATHER_TOKENINIZ
DATABASE_PATH=/data/domain_monitor.db
CHECK_INTERVAL_SECONDS=300
BTK_QUERY_TIMEOUT_SECONDS=300
MAX_CONCURRENT_CHECKS=3
```

## Komutlar

### `/start`

Botu başlatır ve mevcut sohbeti kayıt eder.

### `/add domain.com`

Domaini BTK/ESB takip listesine ekler ve kullanıcıya hemen onay mesajı gönderir. İlk BTK sorgusu arka planda yapılır; sonuç gelince ayrıca mesaj gönderilir.

```text
/add betinebet.com
```

URL girerseniz domain otomatik normalize edilir:

```text
/add https://www.example.com/path
```

Bu kayıt `example.com` olarak saklanır.

### `/remove domain.com`

Domaini takip listesinden çıkarır.

```text
/remove example.com
```

### `/list`

Kayıtlı domainleri, son BTK/ESB durumlarını ve son kontrol zamanını listeler.

### `/check domain.com`

Domain için hemen manuel BTK/ESB sorgusu yapar. Bu komut domaini listeye eklemez; zaman aşımı olursa daha sonra aynı komutla tekrar kontrol edebilirsiniz.

```text
/check example.com
```

### `/help`

Komut listesini gösterir.

## Loglama

Her komut geldiğinde loga şu formatta kayıt düşer:

```text
command received: /add chat_id=... user_id=...
```

Beklenmeyen komut hatalarında kullanıcıya kısa hata mesajı gönderilir.

## Veritabanı

SQLite dosyası varsayılan olarak proje klasöründe `domain_monitor.db` adıyla oluşur.

Saklanan bilgiler:

- Telegram chat ID
- Domain
- Son durum: `UNKNOWN`, `BLOCKED`, `CLEAR`
- Son kontrol zamanı
- Son sorgu detayı/hata metni

`.gitignore` içinde veritabanı ve `.env` dışlanmıştır. Bunları GitHub'a yüklemeyin.

## Docker

Docker imajı oluşturma:

```bash
docker build -t domain-monitor-bot .
```

Çalıştırma:

```bash
docker run -d \
  --name domain-monitor-bot \
  --env-file .env \
  -v domain_monitor_data:/data \
  domain-monitor-bot
```

Logları izleme:

```bash
docker logs -f domain-monitor-bot
```

## Güvenlik

- `BOT_TOKEN` değerini GitHub'a yüklemeyin.
- Token yanlışlıkla paylaşıldıysa BotFather üzerinden yenileyin.
- Railway'de tokeni sadece Variables bölümüne koyun.
- `.env`, `domain_monitor.db`, log ve cache dosyaları repoya eklenmemelidir.
