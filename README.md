# DerinNLPV2

Otonom GitHub dokümantasyon ajanı: repo URL → kod özeti → Gemini ile README → Pull Request. **FastAPI** backend ve **Vite + React** frontend içerir.

## Çalıştırma

1. `backend/.env.example` dosyasına bakarak `backend/.env` oluşturun (`GITHUB_TOKEN`, `GEMINI_API_KEY`).
2. Tek komut (önerilen): kök dizinde `dev.bat start`
3. Durdurma: kök dizinde `dev.bat stop`
4. Yeniden başlatma: `dev.bat restart`
5. Durum kontrolü: `dev.bat status`

Geliştirmede API istekleri Vite proxy ile `/api` üzerinden backend’e gider.

`dev.bat start`, backend (`8000`) ve frontend (`5173`) portlarını başlatmadan önce eski dinleyicileri temizler; böylece port çakışması birikmez.

## Lisans

Proje dosyaları kullanıcı deposuna göre lisanslanır; bağımlılıkların lisansları kendi paketlerine aittir.
