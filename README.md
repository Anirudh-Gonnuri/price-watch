# price-watch

Paste a product link from Amazon.in or Flipkart, optionally with a price, and
get a phone alert when it's in stock (at or under that price). It runs every
10 minutes from my laptop and from GitHub Actions.

## Using it

```sh
cd ~/price-watch
python3 watch.py add '<link>'            # alert when it's in stock
python3 watch.py add '<link>' 1,05,000   # ...and at or under ₹1,05,000
python3 watch.py list                    # what's watched, last price and status
python3 watch.py remove 2                # by number from list (or by key)
python3 watch.py resume                  # turn checking on (laptop + GitHub)
python3 watch.py pause                   # turn it off, keeping everything
```

- **Links:** full links with tracking junk, `amzn.in/d/…` and `dl.flipkart.com/…`
  share links all work. Use the link of the exact colour/size you want.
  Re-adding a link updates its price.
- **`add` checks the page once** before saving, so a bad or blocked link fails
  right away, and it tells you if the alert condition is already true.
- **Prices** accept `105000`, `1,05,000` or `₹1,05,000`. They're the listed
  price; bank-card and EMI offers differ per account, so they're ignored.
- **Quote the link** (`'…'`), because `&` in links means something to the shell.

The list lives in `targets.json`, which is only on the laptop (the repo is
public). `add` and `remove` copy it to the `WATCH_TARGETS` repo variable, which
the GitHub job reads, so nothing needs committing.

## Alerts

Each target remembers its last status in `state.json` and alerts only when the
status changes. A CAPTCHA or unexpected page never counts as "unavailable":

| Alert | Meaning |
|---|---|
| **Amazon: … is in stock!** / **Flipkart: … is at ₹X (target ₹Y)!** | what you're waiting for has happened |
| **… is listed, not buyable yet** | a watched Amazon variant appeared but can't be bought yet |
| **… is now sold out** / **… is now above your ₹Y target** | it stopped being worth buying |
| **\<Site\> watcher can't see the page** | 6 runs in a row hit a CAPTCHA or bot check (blocked fetches are retried first) |
| **\<Site\> watcher needs updating** | 2 runs in a row got a page without the expected markers: the site redesigned it and the parser needs fixing |
| **Watcher: still running** (low priority, 08:00 IST daily, GitHub only) | the schedule is alive; shows each target's status, price, and how many checks got through. If it stops arriving, check the Actions tab |

## How it works

`watch.py` with no command checks every target once; the schedulers run that.

- **Amazon product** (`asin`): reads the product page's price and whether it
  has an Add to Cart button.
- **Amazon variant not listed yet** (`listing` + `match`): reads a listing's
  variant table and waits for a variant whose labels contain every `match`
  term to appear and be buyable. Trackers like Keepa can't do this, because an
  unlisted variant has no ASIN. `add` doesn't create these; write them by hand
  (see [`targets.example.json`](targets.example.json)).
- **Flipkart product** (`url`): reads the page's schema.org Product data, the
  structured block published for search engines, which changes far less often
  than the visual layout.

## Where it runs

| Runner | When | Alerts |
|---|---|---|
| Windows Task Scheduler (`install-windows-task.ps1`) | every 10 min while the laptop is on | Windows toast + ntfy |
| GitHub Actions (`.github/workflows/watch.yml`) | every 10 min, always | ntfy |

From GitHub's data-centre IPs, Amazon serves a CAPTCHA on roughly 2 of 3 runs;
Flipkart got through 5 of 5 in testing. So for Amazon the laptop is the main
watcher and GitHub the backup; for Flipkart, GitHub alone is reliable. Each
runner keeps its own state, so an event can alert once from each.

## Known limits

- Only amazon.in and flipkart.com.
- Amazon search is behind a JavaScript bot challenge, so an item that shows up
  as a brand-new listing can't be discovered; you need its link. Getting past
  the challenge would mean defeating Amazon's bot protection, which this
  project deliberately doesn't do.
- GitHub disables schedules in repos with no commits for 60 days. The missing
  daily heartbeat is the signal; `python3 watch.py resume` turns it back on.
- The public repo's Actions logs show watched product IDs and prices.

## Setup

- Phone alerts: install the [ntfy](https://ntfy.sh) app and subscribe to the
  topic in `ntfy_topic.txt` (git-ignored). The same value is the `NTFY_TOPIC`
  repo secret.
- Local schedule, from WSL:
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(wslpath -w install-windows-task.ps1)"`
- Test notifications: `python3 watch.py --test-notify`
- Tests (offline): `python3 -m unittest -v`
- Remove the laptop task entirely:
  `Unregister-ScheduledTask -TaskName 'Price Watch' -Confirm:$false` (PowerShell)
