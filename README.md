# DerinNLPV2

Otonom GitHub dokümantasyon ajanı: repo URL → kod özeti → Gemini ile README → Pull Request. **FastAPI** backend ve **Vite + React** frontend içerir.

## Çalıştırma

1. `backend/.env.example` dosyasına bakarak `backend/.env` oluşturun (`GITHUB_TOKEN`, `GEMINI_API_KEY`).
2. Backend: `backend/run_dev.bat` veya `uvicorn main:app --reload --host 127.0.0.1 --port 8000` (`backend` klasöründen).
3. Frontend: `frontend/run_dev.bat` veya `npm run dev` (`frontend` klasöründen).

Geliştirmede API istekleri Vite proxy ile `/api` üzerinden backend’e gider.

## Lisans

Proje dosyaları kullanıcı deposuna göre lisanslanır; bağımlılıkların lisansları kendi paketlerine aittir.
