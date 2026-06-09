# Domain Monitor Telegram Bot

Bu bot, kayıtlı domainleri belirli aralıklarla kontrol eder. Domain erişilemez hale gelirse veya tekrar erişilebilir olursa Telegram üzerinden bildirim gönderir.

Varsayılan kontrol aralığı 5 dakikadır.

## Özellikler

- `/start`
- `/add domain.com`
- `/remove domain.com`
- `/list`
- `/check domain.com`
- `/help`
- Domainleri SQLite veritabanında saklar.
- Kayıtlı domainleri her 5 dakikada bir kontrol eder.
- Sadece durum değiştiğinde bildirim gönderir.
- `BOT_TOKEN` önce Railway environment variable üzerinden, yoksa `.env` dosyasından okunur.
- Railway üzerinde Dockerfile ile çalışmaya hazırdır.

## Önemli Not

Bot domainleri çalıştığı sunucudan kontrol eder. Railway üzerinde çalışıyorsa kontrol Railway ağından yapılır. Bir domain sadece Türkiye içinden engelliyse ama Railway lokasyonundan erişilebiliyorsa bot bunu `OK` görebilir.

Bot şu durumları erişim problemi olarak kabul eder:

- DNS veya bağlantı hatası
- Timeout
- HTTP 5xx
- HTTP 451
- HTTPS ve HTTP denemelerinin ikisinin de başarısız olması

HTTP 404 gibi 4xx yanıtlar genelde domainin erişilebilir olduğunu gösterdiği için `OK` kabul edilir. `451` bunun istisnasıdır.

## Kurulum

Python 3.10 veya daha yeni bir sürüm gerekir.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Windows PowerShell için:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

`.env` dosyasını doldurun:

```env
BOT_TOKEN=BOTFATHER_TOKENINIZ
DATABASE_PATH=domain_monitor.db
CHECK_INTERVAL_SECONDS=300
REQUEST_TIMEOUT_SECONDS=15
```

Botu çalıştırın:

```bash
python main.py
```

## Railway Kurulumu

1. Projeyi GitHub'a yükleyin.
2. Railway'de yeni proje oluşturun.
3. GitHub reposunu Railway'e bağlayın.
4. Railway Variables bölümüne şunu ekleyin:

```env
BOT_TOKEN=BOTFATHER_TOKENINIZ
```

İsteğe bağlı değişkenler:

```env
CHECK_INTERVAL_SECONDS=300
REQUEST_TIMEOUT_SECONDS=15
DATABASE_PATH=/data/domain_monitor.db
```

Kalıcı SQLite verisi için Railway'de volume ekleyin ve `/data` yoluna mount edin. Volume kullanmazsanız Railway deploy/restart sonrası SQLite dosyası kaybolabilir.

Railway'de tek instance/replica çalıştırın. Birden fazla instance aynı domainleri kontrol ederse çift bildirim gönderebilir.

## Komutlar

### `/start`

Botu başlatır ve mevcut sohbeti kayıt eder.

### `/add domain.com`

Domaini izleme listesine ekler.

```text
/add example.com
```

URL girerseniz domain otomatik normalize edilir:

```text
/add https://www.example.com/path
```

Bu kayıt `example.com` olarak saklanır.

### `/remove domain.com`

Domaini izleme listesinden çıkarır.

```text
/remove example.com
```

### `/list`

Kayıtlı domainleri, son durumlarını ve son kontrol zamanını listeler.

### `/check domain.com`

Domaini hemen kontrol eder. Bu komut domaini listeye eklemez.

```text
/check example.com
```

### `/help`

Komut listesini gösterir.

## Veritabanı

SQLite dosyası varsayılan olarak proje klasöründe `domain_monitor.db` adıyla oluşur.

Saklanan bilgiler:

- Telegram chat ID
- Domain
- Son durum: `UNKNOWN`, `UP`, `DOWN`
- Son kontrol zamanı
- Son hata detayı

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
