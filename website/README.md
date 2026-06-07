# SwifPro BI - marketing website (swifprobi.com)

A standalone, static marketing site for **swifprobi.com**, separate from the ERP
application. No build step or server required - just HTML/CSS with Bootstrap and
Bootstrap Icons from a CDN, using the SwifPro BI brand palette (navy + teal +
gold) with light/dark mode.

```
website/
  index.html        # the one-page site (hero, features, modules, preview, pricing, contact, footer)
  assets/           # logo + wordmark (light & dark)
```

## Preview locally
Open `website/index.html` directly in a browser, or serve the folder:
```bash
cd website
python -m http.server 5500
# visit http://127.0.0.1:5500/
```

## Deploy to swifprobi.com
It's just static files - host them anywhere:
- **Namecheap shared hosting:** upload the contents of `website/` to `public_html/`.
- **Netlify / Vercel / Cloudflare Pages / GitHub Pages:** point the project at this `website/` folder; set your domain `swifprobi.com`.
Then set the domain's DNS at Namecheap to your host.

## Two things to wire up
1. **Demo/contact form** - `index.html` uses **Netlify Forms** (`data-netlify="true"`,
   form name `demo`), so it works automatically when deployed on Netlify - no third-party
   service or form ID needed. Submissions appear under **Netlify dashboard -> Forms** and
   redirect to `thank-you.html`. To get them emailed to `hello@swifprobi.com`:
   Netlify -> Site -> **Forms -> Form notifications -> Add notification -> Email notification**.
   (Form detection is on by default; if a deploy doesn't pick it up, enable
   **Forms -> Form detection** and redeploy.)
2. **"Sign in" links** - they point to `https://app.swifprobi.com/login/`. Change
   these to wherever you host the ERP app (e.g. a subdomain `app.swifprobi.com`,
   or `swifprobi.com/login/` if the app shares the domain).

## Customising
- Colours live in the `:root` / `[data-bs-theme="dark"]` blocks at the top of
  `index.html` (same tokens as the app).
- Replace pricing, copy and email (`hello@swifprobi.com`) with your own.
