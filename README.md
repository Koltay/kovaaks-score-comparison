# KovaaK's Score Comparison

A Windows desktop app for comparing two players' KovaaK's benchmark scores.

## Requirements

- Python 3.10 or later
- Tkinter (included with most Windows Python installations)

Install the Python dependency:

```powershell
py -m pip install -r requirements.txt
```

## Run

```powershell
py app.py
```

On first launch, the app creates `config.json` beside `app.py`. Open the **Config** tab and enter two Steam IDs, Steam profile URLs, or EVXL profile URLs (for example, `https://evxl.app/u/<17-digit-SteamID>`). For an EVXL URL, the app reads the 17-digit Steam ID immediately after `/u/`, including when the URL continues to a scenario page. The app saves those IDs locally in `config.json`; Git ignores that file. `config.example.json` shows the blank structure without personal values.

In **Config**, enter your KovaaK's web-app username/email and password and select **Get token**. The app clears both credential fields as soon as the request starts. On success, the token appears masked in a read-only field and stays in memory for the current app session; it is not saved. Start the comparison from **Main**. If token retrieval fails, the app shows an error and you can enter your credentials again to retry.

This login flow is based on an [unofficial KovaaK's API wrapper](https://github.com/333moxy/kovaaker/blob/70a76ced30ec1498d5b5b1dc9bc4c4367e3b2d10/kovaaker.py), not official KovaaK's documentation. The endpoint may change, so verify it works before relying on it.

The app fetches benchmark-list pages in batches of 300 using Player A's Steam ID, then requests scores for both players. Requests are paced and run with limited concurrency. If KovaaK's responds with HTTP 429, the app pauses and retries according to the `Retry-After` header when provided, with bounded backoff retries otherwise. It shows progress and keeps completed results if you stop the comparison.

The **Debug** tab groups results by benchmark name. Expand a benchmark to see each scenario and both players' scores. Use the search field and **Errors only** filter in the toolbar to find relevant messages; benchmark details stay collapsed until you expand them. Fetch results and errors include local timestamps and HTTP status details.
