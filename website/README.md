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
1. **Demo/contact form** - `index.html` posts to a placeholder. Create a free
   endpoint (e.g. https://formspree.io) and replace `https://formspree.io/f/your-form-id`
   in the `<form action=...>`. (Or swap it for your own backend.)
2. **"Sign in" links** - they point to `https://app.swifprobi.com/login/`. Change
   these to wherever you host the ERP app (e.g. a subdomain `app.swifprobi.com`,
   or `swifprobi.com/login/` if the app shares the domain).

## Customising
- Colours live in the `:root` / `[data-bs-theme="dark"]` blocks at the top of
  `index.html` (same tokens as the app).
- Replace pricing, copy and email (`hello@swifprobi.com`) with your own.
