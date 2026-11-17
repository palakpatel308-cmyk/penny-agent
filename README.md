# Penny Agent (free Scavenger-style deal finder)

Watches for Home Depot penny items ($0.01) and cheap Home Depot / Lowe's clearance,
then pushes alerts to your phone. Costs $0. Runs every 3 hours on GitHub.

## What it watches

| Source | What you get |
|---|---|
| PennyCentral Penny List (free, community) | New Home Depot $0.01 SKUs. Florida finds are marked **IN YOUR STATE** and sent as high priority. |
| Slickdeals RSS | Lowe's and Home Depot posts mentioning "penny" or priced $5 or less. |

Each Home Depot alert opens `homedepot.com/s/<SKU>` so you can check your local store's stock.

## Setup (about 10 minutes, works from a phone)

1. **Get alerts app:** Install **ntfy** (free, iOS/Android). Tap **+**, subscribe to a topic
   with a hard-to-guess name, like `paul-penny-8k3x92`. Anyone who knows the name can read it, so keep it random.
2. **Make a GitHub account** (free) at github.com if you don't have one.
3. **Create a new repository** named `penny-agent`. Public = unlimited free runs. Private also works (2,000 free minutes/month; this uses about 250).
4. **Upload the files** from this zip (Add file > Upload files). Keep the `.github/workflows/penny.yml` folder path.
5. **Add your topic as a secret:** Settings > Secrets and variables > Actions > New repository secret.
   Name: `NTFY_TOPIC`, Value: your topic name.
6. **Run it once:** Actions tab > Penny Agent > Run workflow. You should get a "Penny Agent is live" push.
   After that, you only get alerts for new finds.

## Tweak it (`config.json`)

- `home_states`: states that get high-priority alerts. Default `["FL"]`.
- `nearby_states`: ranked second.
- `alert_all_states`: `false` = only alert when your state reports it.
- `min_original_price`: skip junk, e.g. `20` = only items that were $20+.
- `slickdeals_max_price`: price cutoff for Lowe's / HD clearance posts.

## Troubleshooting

- **"0 items parsed" in the Actions log:** the penny list site changed its layout.
  Run locally with `python penny_agent.py --debug --dry-run` and look at `debug/penny_list.html`.
- **No alerts for a while:** normal. Penny drops come in waves. Check the Actions tab for green runs.
- **Test without sending anything:** `python penny_agent.py --dry-run`

## Reality check

- Home Depot hides $0.01 prices from its website and app. Every penny tool (paid ones too) depends on
  shoppers scanning in stores and reporting what they find. This agent uses the free community list.
- Always confirm with a scan at self-checkout. Stores can pull penny items or refuse the sale.
- Lowe's rarely does true penny pricing. Expect deep clearance (90%+ off) instead.
