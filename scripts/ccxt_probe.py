"""Probe ccxt URL structure for binanceusdm (offline, no keys, no network)."""
import ccxt

print("ccxt", ccxt.__version__)
ex = ccxt.binanceusdm({"apiKey": "x", "secret": "y", "enableRateLimit": True})
urls = ex.urls
print("top_keys", sorted(urls.keys()))
api = urls.get("api", {})
if isinstance(api, dict):
    for k in sorted(api.keys()):
        print("api", k, "=", api[k])
else:
    print("api =", api)
print("test =", urls.get("test"))
print("demo =", urls.get("demo"))
