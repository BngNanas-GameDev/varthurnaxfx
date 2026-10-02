# TradeAgent — Execution / Security / Ops (Worker 3)

BTCUSDT-PERP, isolated 10x, risk 1%/trade, daily -5% halt, SL+TP native wajib,
idempotency `clientOrderId=trace_id`, 1 posisi 1 arah, API trade-only + IP whitelist.

Worker lain memiliki `src/orchestrator/*`, `src/common/*`, `src/data/*`,
`src/strategy/*`, `configs/models.json`, `configs/risk.json` — jangan sentuh.

## Struktur (area worker 3)
- `src/execution/binance_client.py` — wrapper ccxt/testnet + DRY_RUN
- `src/execution/order_guard.py` — validasi pre-send, return `(ok, reason)`
- `src/security/keys.py` — load env/vault, `mask_key()`, validasi no-withdraw
- `src/security/audit_checklist.md` — checklist pre-mainnet
- `src/ops/daemon.py` — loop 24/7 (poll, WS-stale 10s, killswitch)
- `src/ops/alerts.py` — stub Telegram/Discord webhook env
- `tests/` — `test_sizing_guard.py`, `test_killswitch.py`
- `runbooks/HALT_RESUME.md`, `runbooks/GO_LIVE.md`

## Cara jalan

### Test (testnet keys)
```powershell
pip install -r requirements-test.txt
pytest -q
```
```powershell
$env:BINANCE_TESTNET="true"; $env:DRY_RUN="false"
$env:BINANCE_API_KEY="testnet-key"; $env:BINANCE_API_SECRET="testnet-secret"
python -m src.ops.daemon
```

### Paper (log saja, tanpa kirim order)
```powershell
$env:DRY_RUN="true"
python -m src.ops.daemon
```

### Live kecil
1. Selesaikan `src/security/audit_checklist.md` + `runbooks/GO_LIVE.md`.
2. Key live trade-only, IP whitelist, `ALLOW_LIVE=true`, `BINANCE_TESTNET=false`, `DRY_RUN=false`.
3. `docker compose up --build -d` lalu monitor log + alert.

### Docker
```powershell
docker compose up --build        # DRY_RUN default true, mount configs+data
docker compose up --build -d
docker compose logs -f
```
Container `restart: unless-stopped` = auto-restart 24/7.
