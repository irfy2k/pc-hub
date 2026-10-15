# PC Hub

Local Windows dashboard for health checks, disk cleanup and startup apps. Python standard library only.

```
python pchub.py            # opens http://127.0.0.1:8765
python pchub.py --selftest
```

The server binds to 127.0.0.1, checks the Host header, and requires a per-run token on every API call.
